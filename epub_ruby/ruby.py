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


def _segment_text_with_llm_readings(
    text: str, llm_readings: dict[str, str]
) -> Iterator[str | Tuple[str, str]]:
    """Annotate text using exact LLM reading substrings.

    The LLM may return compound keys such as ``文庫本`` instead of
    the tokenized pieces ``文庫`` and ``本``.  Match longest substrings
    first so the correct compound reading is preserved.
    """
    if not llm_readings:
        yield text
        return

    keys = sorted(llm_readings.keys(), key=len, reverse=True)
    buffer: list[str] = []
    i = 0
    while i < len(text):
        match = None
        for key in keys:
            if text.startswith(key, i):
                match = key
                break

        if match is not None:
            if buffer:
                yield "".join(buffer)
                buffer = []
            yield (match, llm_readings[match])
            i += len(match)
            continue

        buffer.append(text[i])
        i += 1

    if buffer:
        yield "".join(buffer)


def generate_readings_llm(
    sentence: str, llm_reader: "LLMRubyReader",
) -> Iterator[str | Tuple[str, str]]:
    """Like :func:`generate_readings`, but lets the LLM self-tokenize.

    The LLM receives the full sentence and returns the exact substrings
    that should receive ruby annotation.  This avoids relying on fugashi
    token boundaries for the final ruby output.
    """
    llm_readings = llm_reader.get_readings(sentence, [])
    if llm_readings:
        yield from _segment_text_with_llm_readings(sentence, llm_readings)
        return

    # Fall back to fugashi-based annotation if the LLM returns nothing.
    for token in generate_readings(sentence):
        yield token


# ---------------------------------------------------------------------------
# Batch helpers (used by RubySoup in LLM mode)
# ---------------------------------------------------------------------------


# Type alias: pre-classified word info for Pass 1 → Pass 2 reuse
_ClassifiedWord = Tuple[str, bool, str | None]  # (surface, needs_ruby, reading)


def _classify_and_collect(
    tagger: Tagger, text: str,
) -> Tuple[List[str], List[_ClassifiedWord]]:
    """Tokenize *text*, return words needing LLM annotation AND full classification.

    Returns:
        ``(word_list, classified)`` where *word_list* is the list of
        words (surface forms) that need ruby annotation, and *classified*
        can be reused in pass 2 to avoid re-tokenizing.

    Note: katakana loanwords with English lemma readings are excluded
    from *word_list* (they are annotated directly by fugashi).
    """
    word_list: List[str] = []
    classified: List[_ClassifiedWord] = []
    for word in tagger(text):
        surface, needs_ruby, reading = _classify_word(word)
        classified.append((surface, needs_ruby, reading))
        if needs_ruby and reading:
            # Skip katakana loanwords (lemma-based English readings)
            if not any("\u3040" <= ch <= "\u309F" for ch in reading):
                continue
            word_list.append(surface)
    return word_list, classified


def _collect_words_for_llm(tagger: Tagger, text: str) -> List[str]:
    """Tokenize *text* and return words that need LLM annotation."""
    word_list, _classified = _classify_and_collect(tagger, text)
    return word_list


def _generate_readings_from_cache(
    text: str,
    batch_readings: Dict[str, Dict[str, str]],
    pre_classified: List[_ClassifiedWord] | None = None,
) -> Iterator[str | Tuple[str, str]]:
    """Like :func:`generate_readings` but uses pre-computed LLM batch readings.

    *batch_readings* contains LLM's readings for each sentence.
    Words the LLM did not annotate are left as plain text (no ruby).
    There is NO fugashi fallback in LLM mode unless the LLM returns nothing
    for the sentence.
    """
    llm_readings = batch_readings.get(text, {})

    if llm_readings:
        yield from _segment_text_with_llm_readings(text, llm_readings)
        return

    if pre_classified is not None:
        # Fast path: reuse pass-1 classification
        for surface, needs_ruby, _fugashi_reading in pre_classified:
            if needs_ruby:
                reading = llm_readings.get(surface)
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
        surface, needs_ruby, _fugashi_reading = _classify_word(word)
        if needs_ruby:
            reading = llm_readings.get(surface)
            if reading:
                yield from _split_tail(surface, reading)
            else:
                yield surface
        else:
            yield surface

class RubySoup:
    """Injects ``<ruby>`` annotations into BeautifulSoup-parsed HTML content.

    Args:
        is_ruby_rp: Whether to emit ``<rp>`` fallback parentheses.
        llm_reader: Optional :class:`LLMRubyReader` for context-aware kanji
            readings via LLM API.  When provided, the LLM annotates ALL
            kanji words purely from context (no dictionary hints).
            There is NO fugashi fallback — failed batches are skipped.
    """

    def __init__(
        self,
        is_ruby_rp: bool = True,
        llm_reader: "LLMRubyReader | None" = None,
    ) -> None:
        self._is_ruby_rp = is_ruby_rp
        self._llm_reader = llm_reader
        # Pre-computed LLM readings: {sentence: {word: reading}}
        self._batch_readings: Dict[str, Dict[str, str]] = {}
        # Pass-1 → Pass-2 classified-word cache: {sentence: [(surface, needs_ruby, reading), ...]}
        self._classified: Dict[str, List[_ClassifiedWord]] = {}

    def inject(self, soup: BeautifulSoup | Tag) -> None:
        """Recursively walk the soup tree and wrap text nodes with ``<ruby>`` tags.

        When using LLM, this does a TWO-PASS approach:
        1. Collect ALL text segments from the tree
        2. Make batch API calls (LLM self-tokenizes and annotates substrings)
        3. Apply LLM readings to every text node
        """
        # --- Pass 1: collect all sentences for LLM self-tokenization ---
        if self._llm_reader is not None:
            items: List[tuple[str, List[str]]] = []
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
        items: List[tuple[str, List[str]]],
    ) -> None:
        """Walk the tree and collect sentence segments for LLM annotation.

        In self-tokenization mode the LLM receives only the sentence text and
        decides which substrings should receive furigana.  Fugashi is no longer
        used to pre-split tokens for the LLM.
        """
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
                    items.append((segment, []))
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
            # Use pre-computed LLM readings + pass-1 classified cache
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
