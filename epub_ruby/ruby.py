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
from typing import TYPE_CHECKING, Iterator, Tuple

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Script, Stylesheet, Tag, TemplateString
from fugashi import Tagger

if TYPE_CHECKING:
    from .llm_ruby import LLMRubyReader

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
# LLM-based reading generation (optional)
# ---------------------------------------------------------------------------


def generate_readings_llm(
    sentence: str, llm_reader: "LLMRubyReader",
) -> Iterator[str | Tuple[str, str]]:
    """Like :func:`generate_readings`, but uses DeepSeek LLM for kanji readings.

    Tokenization is still done by fugashi (reliable word boundaries).
    Only words that *may* need ruby are sent to the LLM for context-aware
    reading resolution.  Katakana loanwords still use fugashi's lemma.
    """
    tagger = _get_tagger()
    words = list(tagger(sentence))

    # Collect words needing ruby
    words_for_llm: list[str] = []
    word_entries: list[tuple[str, bool, str | None]] = []

    for word in words:
        surface, needs_ruby, reading = _classify_word(word)
        word_entries.append((surface, needs_ruby, reading))
        if needs_ruby:
            words_for_llm.append(surface)

    # Get LLM readings (one API call for the whole sentence)
    llm_readings: dict[str, str] = {}
    if words_for_llm:
        llm_readings = llm_reader.get_readings(sentence, words_for_llm)

    # Yield results: prefer LLM reading, fall back to fugashi
    for surface, needs_ruby, fugashi_reading in word_entries:
        if needs_ruby:
            reading = llm_readings.get(surface, fugashi_reading)
            if reading:
                yield from _split_tail(surface, reading)
            else:
                yield surface
        else:
            yield surface


# ---------------------------------------------------------------------------
# Batch helpers (used by RubySoup in LLM mode)
# ---------------------------------------------------------------------------


# Type alias: pre-classified word info for Pass 1 → Pass 2 reuse
_ClassifiedWord = Tuple[str, bool, str | None]  # (surface, needs_ruby, reading)


def _classify_and_collect(
    tagger: Tagger, text: str,
) -> Tuple[Dict[str, str], List[_ClassifiedWord]]:
    """Tokenize *text*, return both fugashi readings AND full classification.

    Returns:
        ``(fugashi_dict, classified)`` where *fugashi_dict* is
        ``{word: fugashi_reading}`` for words needing LLM disambiguation,
        and *classified* can be reused in pass 2 to avoid re-tokenizing.
    """
    fugashi_dict: Dict[str, str] = {}
    classified: List[_ClassifiedWord] = []
    for word in tagger(text):
        surface, needs_ruby, reading = _classify_word(word)
        classified.append((surface, needs_ruby, reading))
        if needs_ruby and reading:
            # Skip katakana loanwords (lemma-based English readings)
            if not any("\u3040" <= ch <= "\u309F" for ch in reading):
                continue
            fugashi_dict[surface] = reading
    return fugashi_dict, classified


def _collect_words_for_llm(tagger: Tagger, text: str) -> List[str]:
    """Tokenize *text* and return words that need LLM-assisted reading."""
    fugashi_dict, _classified = _classify_and_collect(tagger, text)
    return list(fugashi_dict.keys())


def _generate_readings_from_cache(
    text: str,
    batch_readings: Dict[str, Dict[str, str]],
    pre_classified: List[_ClassifiedWord] | None = None,
) -> Iterator[str | Tuple[str, str]]:
    """Like :func:`generate_readings` but uses pre-computed LLM batch readings.

    *batch_readings* contains LLM **corrections** (only words where fugashi
    was wrong).  These are merged on top of fugashi's base readings.
    """
    llm_corrections = batch_readings.get(text, {})

    if pre_classified is not None:
        # Fast path: reuse pass-1 classification
        for surface, needs_ruby, fugashi_reading in pre_classified:
            if needs_ruby:
                # LLM correction overrides fugashi
                reading = llm_corrections.get(surface, fugashi_reading)
                if reading:
                    yield from _split_tail(surface, reading)
                else:
                    yield surface
            else:
                yield surface
        return

    # Slow path: tokenize from scratch
    tagger = _get_tagger()
    for word in tagger(text):
        surface, needs_ruby, fugashi_reading = _classify_word(word)
        if needs_ruby:
            reading = llm_corrections.get(surface, fugashi_reading)
            if reading:
                yield from _split_tail(surface, reading)
            else:
                yield surface
        else:
            yield surface


# ---------------------------------------------------------------------------
# Ruby annotation injection
# ---------------------------------------------------------------------------


class RubySoup:
    """Injects ``<ruby>`` annotations into BeautifulSoup-parsed HTML content.

    Args:
        is_ruby_rp: Whether to emit ``<rp>`` fallback parentheses.
        llm_reader: Optional :class:`LLMRubyReader` for context-aware kanji
            readings via DeepSeek API.  When ``None``, uses fugashi only.
    """

    def __init__(
        self,
        is_ruby_rp: bool = True,
        llm_reader: "LLMRubyReader | None" = None,
    ) -> None:
        self._is_ruby_rp = is_ruby_rp
        self._llm_reader = llm_reader
        # Pre-computed batch readings: {sentence: {word: reading}}
        self._batch_readings: Dict[str, Dict[str, str]] = {}
        # Pass-1 → Pass-2 classified-word cache: {sentence: [(surface, needs_ruby, reading), ...]}
        self._classified: Dict[str, List[_ClassifiedWord]] = {}

    def inject(self, soup: BeautifulSoup | Tag) -> None:
        """Recursively walk the soup tree and wrap text nodes with ``<ruby>`` tags.

        When using LLM, this does a TWO-PASS approach:
        1. Collect ALL text segments and their kanji words from the tree
        2. Make ONE batch API call for ALL segments
        3. Apply readings to every text node
        """
        # --- Pass 1: collect all (sentence, words) pairs (LLM mode only) ---
        if self._llm_reader is not None:
            items: List[tuple[str, Dict[str, str]]] = []
            self._collect_segments(soup, items)
            if items:
                self._batch_readings = self._llm_reader.get_readings_batch(items)
            else:
                self._batch_readings = {}

        # --- Pass 2: apply readings ---
        self._apply(soup)

    # ------------------------------------------------------------------
    # Pass 1: collect text segments (LLM mode)
    # ------------------------------------------------------------------

    def _collect_segments(
        self, soup: BeautifulSoup | Tag,
        items: List[tuple[str, Dict[str, str]]],
    ) -> None:
        """Walk the tree and collect (sentence, {word: fugashi_reading}) pairs.

        Also caches full word classification so Pass 2 can skip tokenization.
        """
        tagger = _get_tagger()
        for child in soup.children:
            if child is None:
                continue
            if isinstance(child, NavigableString) and not isinstance(
                child, (Script, Stylesheet, TemplateString)
            ):
                text = str(child).strip()
                if not text:
                    continue
                for segment in _WHITESPACE_RE.split(text):
                    segment = segment.strip()
                    if not segment:
                        continue
                    fugashi_dict, classified = _classify_and_collect(tagger, segment)
                    if fugashi_dict:
                        items.append((segment, fugashi_dict))
                        self._classified[segment] = classified
            elif isinstance(child, Tag) and child.name not in ("ruby", "rt", "rp"):
                self._collect_segments(child, items)

    # ------------------------------------------------------------------
    # Pass 2: apply readings to text nodes
    # ------------------------------------------------------------------

    def _apply(self, soup: BeautifulSoup | Tag) -> None:
        """Walk tree and replace text nodes with <ruby> tags."""
        for child in soup.children:
            if child is None:
                continue
            if isinstance(child, NavigableString) and not isinstance(
                child, (Script, Stylesheet, TemplateString)
            ):
                if child.strip():
                    self._process_text_node(child)
            elif isinstance(child, Tag) and child.name not in ("ruby", "rt", "rp"):
                self._apply(child)

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
        if self._llm_reader is not None:
            # Use pre-computed batch readings + pass-1 classified cache
            pre = self._classified.get(text)
            readings = _generate_readings_from_cache(text, self._batch_readings, pre)
        else:
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
