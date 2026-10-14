"""Files from other repositories: only the listed ones, with an ETag, cached, stale on failure."""

import io
import urllib.error

import pytest

from supermedicaldoctor.sources import MAX_BYTES, Sources, url, wanted

GITHUB = {
    "name": "genome",
    "kind": "github",
    "repo": "example/health-notes",
    "ref": "main",
    "token_env": "GENOME_TOKEN",
    "files": [
        {"path": "lucia/CURRENT STATE.md", "person": "lucia"},
        {"path": "shared/interactions.md", "person": ["lucia", "pablo"]},
    ],
}
GITEA = {
    "name": "cycle",
    "kind": "gitea",
    "base_url": "http://gitea.example:3000/",
    "repo": "home/lucia-health",
    "files": [{"path": "health/summary.md", "person": "lucia"}],
}


class Response(io.BytesIO):
    def __init__(self, body: bytes, status=200, etag=None):
        super().__init__(body)
        self.status = status
        self.headers = {"ETag": etag} if etag else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Net:
    """A scripted urlopen: each call pops the next answer (a Response or an exception)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def http_error(code):
    return urllib.error.HTTPError("https://x.invalid", code, "x", {}, None)


def test_urls_name_one_file_and_the_ref():
    assert url(GITHUB, "lucia/CURRENT STATE.md") == (
        "https://api.github.com/repos/example/health-notes/contents/lucia/CURRENT%20STATE.md"
        "?ref=main"
    )
    assert url(GITEA, "health/summary.md") == (
        "http://gitea.example:3000/api/v1/repos/home/lucia-health/raw/health/summary.md?ref=main"
    )


def test_only_the_files_of_people_in_scope_are_wanted():
    assert [p for _, p in wanted([GITHUB, GITEA], ["pablo"])] == ["shared/interactions.md"]
    assert len(wanted([GITHUB, GITEA], ["lucia"])) == 3


def test_a_fetch_caches_a_304_confirms_and_a_failure_keeps_the_copy_stale(tmp_path):
    clock = iter([100.0, 200.0, 300.0])
    net = Net(Response(b"# Estado\n", etag='W/"1"'), http_error(304), OSError("down"))
    s = Sources(tmp_path, urlopen=net, clock=lambda: next(clock), env={"GENOME_TOKEN": "t0k"})
    first = s.fetch(GITHUB, "shared/interactions.md")
    assert (first.key, first.text, first.fetched, first.stale) == (
        "sources/genome/shared/interactions.md",
        "# Estado\n",
        100.0,
        False,
    )
    second = s.fetch(GITHUB, "shared/interactions.md")
    assert second.text == "# Estado\n" and second.fetched == 200.0 and not second.stale
    assert net.requests[1].get_header("If-none-match") == 'W/"1"'
    third = s.fetch(GITHUB, "shared/interactions.md")
    assert third.text == "# Estado\n" and third.stale and third.fetched == 200.0
    req = net.requests[0]
    assert req.get_header("Authorization") == "token t0k"
    assert "t0k" not in req.full_url
    assert req.get_header("Accept") == "application/vnd.github.raw"


def test_a_file_over_two_megabytes_is_refused(tmp_path):
    s = Sources(tmp_path, urlopen=Net(Response(b"x" * (MAX_BYTES + 1))), env={})
    got = s.fetch(GITEA, "health/summary.md")
    assert got.text is None and got.stale


def test_an_error_status_leaves_nothing_without_a_cache(tmp_path):
    s = Sources(tmp_path, urlopen=Net(http_error(404)), env={})
    got = s.fetch(GITEA, "health/summary.md")
    assert got.text is None and got.stale and got.fetched is None


@pytest.mark.parametrize("bad", [b"\xff\xfe"])
def test_a_file_that_is_not_text_is_refused(tmp_path, bad):
    s = Sources(tmp_path, urlopen=Net(Response(bad)), env={})
    assert s.fetch(GITEA, "health/summary.md").text is None


def test_status_lists_every_file_with_its_time(tmp_path):
    s = Sources(tmp_path, urlopen=Net(Response(b"ok")), clock=lambda: 50.0, env={})
    s.fetch(GITEA, "health/summary.md")
    assert s.status([GITEA, GITHUB]) == [
        ("cycle/health/summary.md", 50.0),
        ("genome/lucia/CURRENT STATE.md", None),
        ("genome/shared/interactions.md", None),
    ]
