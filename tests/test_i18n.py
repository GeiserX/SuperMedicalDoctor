import re
import string
from datetime import date

import pytest

from supermedicaldoctor import i18n, records
from supermedicaldoctor.i18n import Translator, _load

ALLOWED_TAG = re.compile(r"</?(b|i|u|s|code)>")
ENTITY = re.compile(r"&(lt|gt|amp|quot);")


def walk(value, path=""):
    """Every string in a locale value, with where it sits."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, f"{path}[{i}]")


def placeholders(s: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(s) if field is not None}


def shape(value):
    """The structure of a locale value with each string replaced by its placeholders."""
    if isinstance(value, str):
        return placeholders(value)
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return ("list", len(value))  # word lists may differ in content, not in kind
    return type(value).__name__


def _paired(s: str, opening: str, closing: str) -> bool:
    """Every `closing` has its own `opening` before it, and none is left open."""
    depth = 0
    for ch in s:
        depth += (ch == opening) - (ch == closing)
        if depth < 0:
            return False
    return depth == 0


def inverted_marks(strings: dict) -> list[str]:
    """Spanish strings where a «?» lacks its own «¿» before it, or a «!» its «¡»."""
    bad = []
    for key, value in strings.items():
        for path, s in walk(value, key):
            if not (_paired(s, "¿", "?") and _paired(s, "¡", "!")):
                bad.append(path)
    return bad


# --- the files -----------------------------------------------------------------------------


def test_english_and_spanish_ship():
    for locale in ("en", "es"):
        assert (i18n.LOCALES / f"{locale}.yaml").is_file()


@pytest.mark.parametrize("locale", ["es"])
def test_every_english_key_exists_with_the_same_placeholders(locale):
    en, other = _load("en"), _load(locale)
    assert set(en) == set(other)
    for key in en:
        assert shape(en[key]) == shape(other[key]), key


@pytest.mark.parametrize("locale", ["en", "es"])
def test_no_message_says_what_happens_without_an_answer(locale):
    # an untapped offer just expires; no message adds a line saying so
    for path, s in walk(_load(locale)):
        assert "no respondes" not in s.lower() and "do not answer" not in s.lower(), path


def test_the_shape_check_can_fail():
    """Positive control: a placeholder renamed in a translation is caught."""
    assert shape("{m}, cuéntame") != shape("{name}, cuéntame")


def test_spanish_questions_and_exclamations_open():
    assert inverted_marks(_load("es")) == []


def test_inverted_marks_check_can_fail():
    """Positive control: the check above reports a broken string, and a fixed one passes."""
    assert inverted_marks({"x": "Ha llegado?"}) == ["x"]
    assert inverted_marks({"x": {"y": ["Qué bien!"]}}) == ["x.y[0]"]
    assert inverted_marks({"x": "¿Algo más? Te duele?"}) == ["x"]
    assert inverted_marks({"x": "¿Ha llegado? ¡Qué bien! ¿Te duele?"}) == []


@pytest.mark.parametrize("locale", ["en", "es"])
def test_strings_are_safe_html(locale):
    for path, s in walk(_load(locale)):
        assert "<" not in ALLOWED_TAG.sub("", s), path
        assert "&" not in ENTITY.sub("", s), path


def test_every_slot_has_a_label_and_the_menu_every_command():
    from supermedicaldoctor.bot import MENU

    for locale in ("en", "es"):
        assert set(_load(locale)["slots"]) == set(records.SLOTS)
        assert set(_load(locale)["menu"]) == set(MENU)


def test_every_key_the_code_uses_exists():
    """Each literal key the package passes to the translator is in en.yaml (an f-string key is
    checked through its known values)."""
    import pathlib

    root = pathlib.Path(i18n.__file__).parent
    used = set()
    for path in root.glob("*.py"):
        used |= set(re.findall(r"\bt\(\s*\"([a-z_0-9]+)\"", path.read_text(encoding="utf-8")))
        used |= set(re.findall(r"self\.t\(\s*\"([a-z_0-9]+)\"", path.read_text(encoding="utf-8")))
    en = _load("en")
    for kind in ("medicine", "supplement"):
        used |= {f"{kind}_title", f"{kind}_none", f"kind_{kind}", f"ctx_kind_{kind}"}
    used |= {f"profile_{k}" for k in records.PROFILE_LISTS}
    used |= {f"remind_{k}" for k in ("here", "calendar", "nothing", "timeout")}
    used |= {"settings_followup_on", "settings_followup_off", "ctx_low", "ctx_high"}
    used = {k for k in used if not k.endswith("_")}  # the prefix of a key built at run time
    missing = sorted(k for k in used if k not in en)
    assert missing == []
    assert len(used) > 80  # the scan found the calls: a broken regex would find none


# --- the Translator ------------------------------------------------------------------------


def test_dates_and_slots():
    wednesday = date(2027, 3, 10)
    es, en = Translator("es"), Translator("en")
    assert es.date(wednesday) == "miércoles 10 de marzo"
    assert en.date(wednesday) == "Wednesday 10 March"
    assert es.date(wednesday, "short") == "10 de marzo"
    assert es.slot("waking") == "Al despertar"
    with pytest.raises(ValueError):
        es.date(wednesday, "weird")


def test_the_introduction_is_the_designed_text():
    assert Translator("es")("intro").startswith(
        "Hola, soy el asistente médico de la casa. Llevo vuestras citas, vuestra medicación y "
        "vuestros suplementos"
    )


def test_unknown_locale_falls_back_to_english():
    assert Translator("xx")("undo_none") == "Nothing to undo."
    assert Translator("xx").date(date(2027, 3, 10)) == "Wednesday 10 March"
