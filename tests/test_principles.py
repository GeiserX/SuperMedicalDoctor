"""The shared assistant principles: shipped with the app, unchanged, and read first, once."""

import hashlib

import pytest

from supermedicaldoctor import assistant

# The shared assistant principles, byte for byte. A change to them lands here on purpose, never by
# accident: copy the new text into supermedicaldoctor/principles/ and update the hash.
SHA256 = {
    "en": "1132dd4ab7570d7bdf96146532e2f8d71052091ef657c9332aff5a9fe3a6cae3",
    "es": "1d47e3c8292543d302add5d0caab2ccda46b95ec1ba49d661d035f6f31e8c312",
}
HEADING = {"en": "## Principles", "es": "## Principios"}
PEOPLE = {"lucia": "Lucía", "pablo": "Pablo"}


@pytest.mark.parametrize("lang", sorted(SHA256))
def test_the_shipped_principles_are_the_shared_text(lang):
    raw = (assistant.PRINCIPLES / f"{lang}.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA256[lang]


@pytest.mark.parametrize("lang", sorted(SHA256))
def test_the_rules_start_with_the_twelve_principles_exactly_once(lang):
    rules = assistant.rules(PEOPLE, lang, "Prefer short answers.")
    text = (assistant.PRINCIPLES / f"{lang}.md").read_text(encoding="utf-8").rstrip()
    assert text.count("**") == 24  # twelve bold titles
    assert rules.startswith(text + "\n")
    assert rules.count(HEADING[lang]) == 1 and rules.count(text) == 1
    assert rules.index("You are the household's doctor assistant") > len(text)
    assert rules.rstrip().endswith("Prefer short answers.")
    assert "Lucía (id lucia) and Pablo (id pablo)" in rules
    assert "Person ids: lucia, pablo." in rules


def test_a_language_without_a_copy_reads_the_english_principles():
    assert assistant.rules(PEOPLE, "de").startswith(HEADING["en"])
    assert assistant.rules(PEOPLE, "es-ES").startswith(HEADING["es"])


def test_the_bots_rules_keep_only_what_is_specific_to_it():
    own = assistant.rules(PEOPLE, "en").split("You are the household's doctor assistant", 1)[1]
    own = " ".join(own.split()).lower()
    for gone in (
        "act only on what is addressed to you",
        "is not a request",
        "answer them all in one message",
        "never claim something is saved",
        "never mention files, tools, blocks",
        "a tap is an action",
        "never the same offer twice",
        "if you do not answer, i do nothing",
        'ask your doctor" is not an answer',
    ):
        assert gone not in own, gone
    for kept in ("llama al 112", "evidencia: fuerte", "never change a prescribed dose"):
        assert kept in own, kept


def test_the_specific_rules_check_can_fail():
    """Positive control: a sentence copied from the principles into the rules is caught."""
    copied = "You are the doctor assistant. Act only on what is addressed\n  to you."
    own = " ".join(copied.split()).lower()
    assert "act only on what is addressed to you" in own
