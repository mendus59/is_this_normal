"""Repair text from PDFs whose fonts map the fi and fl ligatures to a bare "f".

The Supreme Court's preliminary prints and bound volumes extract as "offcer", "affrmed",
"frst", "confict" with every PDF library, because the PDF itself says so. A word is
repaired only when it is unknown or rare and putting an "i" or "l" back after one of its
f's gives a common word (counts in data/f_words.txt, taken from clean sources). Text that
shows no sign of the problem is returned unchanged, so clean text is never touched.
"""

import functools
import re
from importlib import resources

_WORD = re.compile(r"[A-Za-z]+")
# Unknown words get repaired when the repair is this common; known ones need this ratio.
MIN_COUNT = 5
RATIO = 50
# A text needs this many repairable words before any repair is made.
MIN_SIGNS = 2
# "fled" is a real word, but in these prints it nearly always means "filed".
_KEEP = {"fled"}
_FLED = re.compile(r"\b([Ff])led(?=\s+(?:a|an|by|suit|briefs?|its|his|her|their|for|on|in|with|this|amicus)\b)")


@functools.cache
def counts() -> dict[str, int]:
    out = {}
    for line in resources.files("itn").joinpath("data/f_words.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            word, n = line.split()
            out[word] = int(n)
    return out


def _candidates(word: str):
    for i, ch in enumerate(word):
        if ch == "f":
            for add in ("i", "l"):
                yield word[: i + 1] + add + word[i + 1:]


@functools.cache
def repair_word(lower: str) -> str | None:
    """The repaired form of a lowercase word, or None when it should stay as it is."""
    if len(lower) < 3 or "f" not in lower or lower in _KEEP:
        return None
    vocab = counts()
    have = vocab.get(lower, 0)
    best = None
    for cand in _candidates(lower):
        n = vocab.get(cand, 0)
        if n >= MIN_COUNT and n >= RATIO * max(have, 1) and (best is None or n > vocab[best]):
            best = cand
    return best


def _cased(original: str, repaired: str) -> str:
    if original.isupper() and len(original) > 1:
        return repaired.upper()
    if original[0].isupper():
        return repaired[0].upper() + repaired[1:]
    return repaired


def signs(text: str) -> int:
    """How many distinct words in the text look like lost ligatures."""
    vocab = counts()
    return len({w.lower() for w in (m.group(0) for m in _WORD.finditer(text or ""))
                if not (w.isupper() and len(w) <= 4) and w.lower() not in vocab and repair_word(w.lower())})


def fix(text: str) -> str:
    """The text with lost fi/fl ligatures put back, or unchanged when it shows no sign of
    the problem."""
    if not text or signs(text) < MIN_SIGNS:
        return text

    def sub(m):
        word = m.group(0)
        if word.isupper() and len(word) <= 4:  # an abbreviation such as "FRE"
            return word
        rep = repair_word(word.lower())
        return _cased(word, rep) if rep else word

    text = _FLED.sub(lambda m: m.group(1) + "iled", text)
    return _WORD.sub(sub, text)
