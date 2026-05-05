# Based on: https://github.com/yihong0618/epubhv
# Furigana engine based on: https://github.com/Mumumu4/furigana4epub
"""
Japanese furigana/ruby annotation engine for EPUB.

Supports:
  - Kanji -> hiragana reading
  - Katakana loanwords -> English reading (via lemma)
"""

from __future__ import annotations

import re
from functools import lru_cache
from itertools import groupby
from typing import Iterator, Tuple

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Script, Stylesheet, Tag, TemplateString
from fugashi import Tagger

# ---------------------------------------------------------------------------
# Character mapping tables (katakana <-> hiragana)
# ---------------------------------------------------------------------------

_KATAKANA_CHART = (
    "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾ"
    "タダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポ"
    "マミムメモャヤュユョヨラリルレロヮワヰヱヲンヴヵヶヽヾ"
)
_HIRAGANA_CHART = (
    "ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞ"
    "ただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽ"
    "まみむめもゃやゅゆょよらりるれろゎわゐゑをんゔゕゖゝゞ"
)

_K2H = str.maketrans(_KATAKANA_CHART, _HIRAGANA_CHART)
_H2K = str.maketrans(_HIRAGANA_CHART, _KATAKANA_CHART)

_WHITESPACE_RE = re.compile(r"(\s+)")
_ENGLISH_ONLY_RE = re.compile(r"[^a-zA-Z\s]")


# ---------------------------------------------------------------------------
# Custom NavigableString subclasses for ruby-related tags
# ---------------------------------------------------------------------------


class RBString(NavigableString):
    """NavigableString subclass for <ruby> contents."""


class RTString(NavigableString):
    """NavigableString subclass for <rt> contents."""


class RPString(NavigableString):
    """NavigableString subclass for <rp> contents."""


# ---------------------------------------------------------------------------
# BeautifulSoup helpers
# ---------------------------------------------------------------------------

_STRING_CONTAINERS = {
    "rp": RPString,
    "rt": RTString,
    "style": Stylesheet,
    "script": Script,
    "template": TemplateString,
}

# Re-export for backward compatibility (used by core.py)
string_containers = _STRING_CONTAINERS


@lru_cache(maxsize=1)
def _get_base_soup() -> BeautifulSoup:
    """Return a cached base BeautifulSoup for tag creation."""
    return BeautifulSoup("<b></b>", "lxml", string_containers=_STRING_CONTAINERS)


# ---------------------------------------------------------------------------
# Tagger (lazy init)
# ---------------------------------------------------------------------------

_tagger: Tagger | None = None


def _get_tagger() -> Tagger:
    """Return the global fugashi Tagger, initializing it on first call."""
    global _tagger
    if _tagger is None:
        _tagger = Tagger()
    return _tagger


# ---------------------------------------------------------------------------
# Text conversion utilities
# ---------------------------------------------------------------------------


def katakana_to_hiragana(text: str) -> str:
    """Convert katakana string to hiragana."""
    return text.translate(_K2H)


def hiragana_to_katakana(text: str) -> str:
    """Convert hiragana string to katakana."""
    return text.translate(_H2K)


# ---------------------------------------------------------------------------
# Word classification
# ---------------------------------------------------------------------------


def _classify_word(word) -> Tuple[str, bool, str | None]:
    """Analyze a fugashi word node and return ``(surface, needs_ruby, reading)``.

    - For katakana loanwords: returns the English reading from lemma.
    - For kanji words: returns the hiragana reading.
    - For kana-only / no-reading words: returns ``(surface, False, None)``.
    """
    surface = word.surface
    kana = word.feature.kana
    lemma = word.feature.lemma or ""

    # Katakana loanword with English gloss (e.g. "コンピュータ-computer")
    if "-" in lemma:
        english = _ENGLISH_ONLY_RE.sub("", lemma.split("-")[1])
        if english:
            return surface, True, english

    # No reading available or surface equals the reading
    if surface == kana or kana in (None, "", "*") or surface in (None, "", "*"):
        return surface, False, None

    hira = katakana_to_hiragana(str(kana))
    if surface == hira:
        return surface, False, None

    return surface, True, hira


# ---------------------------------------------------------------------------
# Reading generation
# ---------------------------------------------------------------------------


def _split_tail(text: str, reading: str) -> Iterator[str | Tuple[str, str]]:
    """Split off the common trailing character between text and reading.

    If the last character matches, yield ``(base_text, base_reading)`` then
    the common suffix as a plain string. Otherwise yield ``(text, reading)``.
    """
    if text[-1] == reading[-1]:
        for i in range(1, min(len(reading), len(text))):
            if text[-i - 1] != reading[-i - 1]:
                yield (text[:-i], reading[:-i])
                yield reading[-i:]
                break
    else:
        yield (text, reading)


def generate_readings(sentence: str) -> Iterator[str | Tuple[str, str]]:
    """Tokenize a Japanese sentence and yield plain strings or ``(text, reading)`` pairs.

    Yields:
        ``str``        – A segment that needs no ruby annotation.
        ``(str, str)`` – A ``(text, reading)`` pair that should be wrapped in ``<ruby>``.
    """
    tagger = _get_tagger()
    for word in tagger(sentence):
        surface, needs_ruby, reading = _classify_word(word)
        if needs_ruby:
            assert reading is not None
            yield from _split_tail(surface, reading)
        else:
            yield surface


# ---------------------------------------------------------------------------
# Ruby annotation injection
# ---------------------------------------------------------------------------


class RubySoup:
    """Injects ``<ruby>`` annotations into BeautifulSoup-parsed HTML content."""

    def __init__(self, is_ruby_rp: bool = True) -> None:
        self._is_ruby_rp = is_ruby_rp

    def inject(self, soup: BeautifulSoup | Tag) -> None:
        """Recursively walk the soup tree and wrap text nodes with ``<ruby>`` tags."""
        for child in soup.children:
            if child is None:
                continue
            if isinstance(child, NavigableString) and not isinstance(
                child, (Script, Stylesheet, TemplateString)
            ):
                if child.strip():
                    self._process_text_node(child)
            elif isinstance(child, Tag) and child.name not in ("ruby", "rt", "rp"):
                self.inject(child)

    # ---- internal helpers --------------------------------------------------

    def _process_text_node(self, node: NavigableString) -> None:
        base = _get_base_soup()
        wrapper = base.new_tag("temptag")
        for segment in _WHITESPACE_RE.split(str(node)):
            if not segment.strip():
                wrapper.append(segment)
            else:
                for item in self._build_ruby_segments(segment):
                    wrapper.append(item)
        node.replace_with(wrapper)
        wrapper.unwrap()

    def _build_ruby_segments(self, text: str) -> Iterator[str | Tag]:
        readings = generate_readings(text)
        for key, group in groupby(readings, key=type):
            if key is str:
                yield "".join(group)
            elif key is tuple:
                yield self._make_ruby_tag(group)

    def _make_ruby_tag(self, pairs) -> Tag:
        base = _get_base_soup()
        ruby_tag = base.new_tag("ruby")
        for text, reading in pairs:
            ruby_tag.append(text)
            rt_tag = base.new_tag("rt")
            rt_tag.append(reading)
            if self._is_ruby_rp:
                rp_open = base.new_tag("rp")
                rp_open.append("(")
                ruby_tag.append(rp_open)
            ruby_tag.append(rt_tag)
            if self._is_ruby_rp:
                rp_close = base.new_tag("rp")
                rp_close.append(")")
                ruby_tag.append(rp_close)
        return ruby_tag
