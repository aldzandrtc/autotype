"""Supported characters, text normalization and the US QWERTY adjacency map.

This module owns everything that depends on the physical keyboard layout: what
can be typed at all, how a character maps onto a :class:`NormalizedKey`, and
which keys sit next to which.  Nothing here knows about timing or typos.
"""

from __future__ import annotations

import string
import unicodedata
from dataclasses import dataclass

from typing_simulator.domain.events import NormalizedKey, SpecialKey
from typing_simulator.errors import UnsupportedCharacterError

# ---------------------------------------------------------------------------
# Supported character set
# ---------------------------------------------------------------------------

#: Punctuation reachable on a US QWERTY keyboard, unshifted and shifted.
SUPPORTED_PUNCTUATION = "`-=[]\\;',./~!@#$%^&*()_+{}|:\"<>?"

SUPPORTED_LETTERS = string.ascii_letters
SUPPORTED_DIGITS = string.digits

#: Every character the first prototype promises to type.  Tabs are deliberately
#: absent: see :func:`describe_unsupported`.
SUPPORTED_CHARACTERS = frozenset(
    SUPPORTED_LETTERS + SUPPORTED_DIGITS + SUPPORTED_PUNCTUATION + " \n"
)


def is_supported(char: str) -> bool:
    return char in SUPPORTED_CHARACTERS


@dataclass(frozen=True, slots=True)
class UnsupportedCharacter:
    """One distinct unsupported character found in the input."""

    char: str
    first_index: int
    count: int

    def describe(self) -> str:
        return f"{describe_unsupported(self.char)} (x{self.count})"


def describe_unsupported(char: str) -> str:
    """A human-readable description of one unsupported character.

    Tabs get a dedicated message because the prototype refuses them on purpose
    rather than converting them to spaces, which would change the user's text.
    """
    if char == "\t":
        return (
            "a tab character - tabs are not supported in this prototype and are "
            "never silently converted; replace them with spaces"
        )
    code = f"U+{ord(char):04X}"
    try:
        name = unicodedata.name(char)
    except ValueError:
        name = "unnamed control character"
    printable = char if char.isprintable() else ""
    shown = f" {printable!r}" if printable else ""
    return f"{code} {name}{shown}"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_line_endings(text: str) -> str:
    """Convert CRLF and lone CR line endings to LF.  Nothing else changes."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def find_unsupported(text: str) -> list[UnsupportedCharacter]:
    """Return the distinct unsupported characters in ``text``, in first-seen order."""
    found: dict[str, UnsupportedCharacter] = {}
    for index, char in enumerate(text):
        if is_supported(char):
            continue
        existing = found.get(char)
        if existing is None:
            found[char] = UnsupportedCharacter(char=char, first_index=index, count=1)
        else:
            found[char] = UnsupportedCharacter(
                char=char, first_index=existing.first_index, count=existing.count + 1
            )
    return list(found.values())


def normalize_text(text: str) -> str:
    """Normalize line endings and reject unsupported characters.

    The returned string is the *exact* text the run must reproduce - no
    characters are added, removed or substituted beyond line-ending
    normalization.
    """
    normalized = normalize_line_endings(text)
    unsupported = find_unsupported(normalized)
    if unsupported:
        raise UnsupportedCharacterError([u.describe() for u in unsupported])
    return normalized


# ---------------------------------------------------------------------------
# Character -> key
# ---------------------------------------------------------------------------


def key_for_character(char: str) -> NormalizedKey:
    """Map a supported character to its normalized key.

    Uppercase and shifted characters are represented by the character itself
    rather than an explicit SHIFT sequence; the backends ask the platform to
    produce that character, which is what works reliably on macOS.
    """
    if char == "\n":
        return NormalizedKey.of(SpecialKey.ENTER)
    if char == " ":
        return NormalizedKey.of(SpecialKey.SPACE)
    if char == "\t":
        return NormalizedKey.of(SpecialKey.TAB)
    if not is_supported(char):
        raise UnsupportedCharacterError([describe_unsupported(char)])
    return NormalizedKey.printable(char)


BACKSPACE_KEY = NormalizedKey.of(SpecialKey.BACKSPACE)
ENTER_KEY = NormalizedKey.of(SpecialKey.ENTER)
SPACE_KEY = NormalizedKey.of(SpecialKey.SPACE)


# ---------------------------------------------------------------------------
# US QWERTY adjacency
# ---------------------------------------------------------------------------

#: The four character rows of a US QWERTY keyboard, with the horizontal offset
#: of each row in key-widths.  The offsets reproduce the physical stagger
#: (Tab is 1.5 units wide, Caps Lock 1.75, left Shift 2.25).
_QWERTY_ROWS: tuple[tuple[str, float], ...] = (
    ("`1234567890-=", 0.00),
    ("qwertyuiop[]\\", 1.50),
    ("asdfghjkl;'", 1.75),
    ("zxcvbnm,./", 2.25),
)

#: Two keys count as neighbours when their centres are within this distance.
_ADJACENCY_RADIUS = 1.25


def _build_adjacency() -> dict[str, tuple[str, ...]]:
    positions: dict[str, tuple[float, float]] = {}
    for row_index, (row, offset) in enumerate(_QWERTY_ROWS):
        for column, char in enumerate(row):
            positions[char] = (offset + column, float(row_index))

    adjacency: dict[str, tuple[str, ...]] = {}
    for char, (x, y) in positions.items():
        neighbours = [
            other
            for other, (ox, oy) in positions.items()
            if other != char
            and abs(oy - y) <= 1
            and ((ox - x) ** 2 + (oy - y) ** 2) ** 0.5 <= _ADJACENCY_RADIUS
        ]
        adjacency[char] = tuple(sorted(neighbours))
    return adjacency


#: Explicit US QWERTY adjacency map, keyed by the unshifted character.
QWERTY_ADJACENCY: dict[str, tuple[str, ...]] = _build_adjacency()


def adjacent_letters(char: str) -> tuple[str, ...]:
    """Neighbouring *letter* keys of ``char``, preserving ``char``'s case.

    Only letters are returned so that an adjacent-key substitution can never
    turn a letter into punctuation, a digit, or anything that would complicate
    the correction plan.
    """
    lowered = char.lower()
    neighbours = QWERTY_ADJACENCY.get(lowered, ())
    letters = tuple(n for n in neighbours if n.isalpha())
    if char.isupper():
        return tuple(n.upper() for n in letters)
    return letters
