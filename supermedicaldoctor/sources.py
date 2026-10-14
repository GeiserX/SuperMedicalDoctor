"""Files from other repositories, fetched before every turn (`settings.sources`).

Only the files a source lists are fetched, one by one, never a directory, so the bot can never
read a folder nobody named. Each request carries the cached `ETag` in `If-None-Match`, so an
unchanged file costs one 304. A fetch that fails keeps the cached copy and marks it stale; a file
over 2 MB is refused. The token named by `token_env` goes in an `Authorization` header, never in
a URL, and nothing here logs a path, a body or a URL.

The cache is `state/sources/<name>/<path>` plus `<path>.meta` (`{"etag", "fetched"}`).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from supermedicaldoctor.records import file_people

log = logging.getLogger("smd.sources")

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 15
CACHE = "state/sources"


@dataclass(frozen=True)
class Fetched:
    key: str  # "sources/<name>/<path>", the file's place in the workspace
    text: str | None  # None: never fetched and nothing cached
    fetched: float | None  # when the cached copy was last confirmed (epoch seconds)
    stale: bool  # this turn's fetch failed and the cached copy is older


def url(src: Mapping, path: str) -> str:
    quoted = urllib.parse.quote(path, safe="/")
    ref = urllib.parse.quote(str(src.get("ref") or "main"), safe="")
    if src["kind"] == "github":
        return f"https://api.github.com/repos/{src['repo']}/contents/{quoted}?ref={ref}"
    base = str(src["base_url"]).rstrip("/")
    return f"{base}/api/v1/repos/{src['repo']}/raw/{quoted}?ref={ref}"


def wanted(sources: Sequence[Mapping], scope: Sequence[str]) -> list[tuple[Mapping, str]]:
    """Every listed file whose `person` meets the turn's scope, as (source, path)."""
    out = []
    for src in sources:
        for f in src.get("files") or []:
            if set(file_people(f)) & set(scope):
                out.append((src, str(f["path"])))
    return out


class Sources:
    def __init__(
        self,
        data: str | Path,
        urlopen: Callable = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
        env: Mapping[str, str] | None = None,
    ):
        self.root = Path(data) / CACHE
        self._urlopen = urlopen
        self._clock = clock
        self._env = env if env is not None else os.environ

    def _paths(self, src: Mapping, path: str) -> tuple[Path, Path]:
        body = self.root / str(src["name"]) / path
        return body, body.with_name(body.name + ".meta")

    def cached(self, src: Mapping, path: str) -> tuple[str | None, dict]:
        body, meta = self._paths(src, path)
        try:
            text = body.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError):
            return None, {}
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            info = {}
        return text, info if isinstance(info, dict) else {}

    def fetch(self, src: Mapping, path: str) -> Fetched:
        """One file: a 200 replaces the cache, a 304 confirms it, anything else keeps it stale."""
        key = f"sources/{src['name']}/{path}"
        text, info = self.cached(src, path)
        headers = {"User-Agent": "SuperMedicalDoctor", "Accept": "application/vnd.github.raw"}
        token = str(self._env.get(str(src.get("token_env") or ""), "") or "").strip()
        if token:
            headers["Authorization"] = f"token {token}"
        if text is not None and info.get("etag"):
            headers["If-None-Match"] = str(info["etag"])
        status, body, etag = 0, b"", None
        try:
            request = urllib.request.Request(url(src, path), headers=headers)
            with self._urlopen(request, timeout=TIMEOUT_S) as response:
                status = getattr(response, "status", 200)
                body = response.read(MAX_BYTES + 1)
                etag = response.headers.get("ETag") if response.headers else None
        except urllib.error.HTTPError as e:
            status = e.code
            e.close()
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("source fetch failed: %s", type(e).__name__)
            return Fetched(key, text, info.get("fetched"), True)
        now = self._clock()
        if status == 304 and text is not None:
            self._save_meta(src, path, info.get("etag"), now)
            return Fetched(key, text, now, False)
        if status != 200:
            log.warning("source fetch failed: HTTP %s", status)
            return Fetched(key, text, info.get("fetched"), True)
        if len(body) > MAX_BYTES:
            log.warning("source refused: too large")
            return Fetched(key, text, info.get("fetched"), True)
        try:
            new = body.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("source refused: not text")
            return Fetched(key, text, info.get("fetched"), True)
        file, _ = self._paths(src, path)
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_name(file.name + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, file)
        self._save_meta(src, path, etag, now)
        return Fetched(key, new, now, False)

    def _save_meta(self, src: Mapping, path: str, etag: object, now: float) -> None:
        _, meta = self._paths(src, path)
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"etag": etag, "fetched": now}), encoding="utf-8")

    def fetch_all(self, sources: Sequence[Mapping], scope: Sequence[str]) -> list[Fetched]:
        out = [self.fetch(src, path) for src, path in wanted(sources, scope)]
        log.info("sources fetched n=%d stale=%d", len(out), sum(1 for f in out if f.stale))
        return out

    def status(self, sources: Sequence[Mapping]) -> list[tuple[str, float | None]]:
        """For /estado: each listed file's workspace key and when it was last confirmed."""
        out = []
        for src in sources:
            for f in src.get("files") or []:
                _, info = self.cached(src, str(f["path"]))
                fetched = info.get("fetched")
                out.append(
                    (
                        f"{src['name']}/{f['path']}",
                        fetched if isinstance(fetched, int | float) else None,
                    )
                )
        return out
