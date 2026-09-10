"""Stable employer-name matching shared by import and query paths."""

import math
import re
import unicodedata
from typing import Any

# These forms occur frequently in the FY2026 Q1 employer data. Canonicalizing
# dotted abbreviations before removing legal suffixes keeps names such as
# "Woven by Toyota, U.S., Inc." and "WOVEN BY TOYOTA US INC" equivalent.
_EMPLOYER_ABBREVIATIONS = (
    (("p", "l", "l", "c"), "pllc"),
    (("g", "m", "b", "h"), "gmbh"),
    (("l", "l", "c"), "llc"),
    (("l", "l", "p"), "llp"),
    (("p", "l", "c"), "plc"),
    (("u", "s", "a"), "usa"),
    (("u", "s"), "us"),
    (("n", "a"), "na"),
    (("p", "c"), "pc"),
    (("p", "a"), "pa"),
    (("l", "p"), "lp"),
    (("s", "a"), "sa"),
    (("a", "g"), "ag"),
    (("b", "v"), "bv"),
    (("n", "v"), "nv"),
)
_EMPLOYER_LEGAL_SUFFIX_PHRASES = (
    ("limited", "liability", "company"),
    ("public", "benefit", "corporation"),
    ("professional", "corporation"),
)
_EMPLOYER_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "llp",
        "lp",
        "pllc",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "pc",
        "pa",
        "pbc",
        "na",
        "sa",
        "ag",
        "bv",
        "nv",
        "gmbh",
    }
)


def _employer_tokens(value: Any) -> list[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []

    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(
        character for character in text if not unicodedata.combining(character)
    )
    tokens = re.findall(r"[a-z0-9]+", text.replace("&", " and "))

    canonical_tokens: list[str] = []
    index = 0
    while index < len(tokens):
        for source, replacement in _EMPLOYER_ABBREVIATIONS:
            if tuple(tokens[index : index + len(source)]) == source:
                canonical_tokens.append(replacement)
                index += len(source)
                break
        else:
            canonical_tokens.append(tokens[index])
            index += 1

    return canonical_tokens


def _normalise_employer(value: Any) -> str:
    tokens = _employer_tokens(value)
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    original_tokens = tokens.copy()

    while len(tokens) > 1:
        removed_suffix = False
        for suffix in _EMPLOYER_LEGAL_SUFFIX_PHRASES:
            if len(tokens) > len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                del tokens[-len(suffix) :]
                removed_suffix = True
                break
        if removed_suffix:
            continue
        if tokens[-1] in _EMPLOYER_LEGAL_SUFFIXES:
            tokens.pop()
            continue
        break

    if not tokens:
        tokens = original_tokens
    return "".join(tokens)
