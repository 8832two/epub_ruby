from __future__ import annotations

import re
import logging
from functools import lru_cache
from itertools import groupby
from typing import TYPE_CHECKING, Iterator, Tuple, List, Dict, Set

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Script, Stylesheet, Tag, TemplateString

if TYPE_CHECKING:
    from fugashi import Tagger
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
    """Return the global fugashi Tagger, initializing it on first call.

    The ``fugashi`` import is deferred so LLM-only users don't need it.
    """
    global _tagger
    if _tagger is None:
        from fugashi import Tagger  # lazy import

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

    Edge case: when *text* and *reading* are fully identical (should not
    happen, but guards against silent text loss), yields ``(text, reading)``
    so the text is at least preserved with a ruby annotation.
    """
    if text[-1] == reading[-1]:
        for i in range(1, min(len(reading), len(text))):
            if text[-i - 1] != reading[-i - 1]:
                yield (text[:-i], reading[:-i])
                yield reading[-i:]
                break
        else:
            # for-loop completed without break → text == reading
            # Yield as-is so the text is NOT silently dropped.
            yield (text, reading)
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


@lru_cache(maxsize=4096)
def _contains_kanji(text: str) -> bool:
    """Return ``True`` if *text* contains at least one CJK unified ideograph."""
    for ch in text:
        cp = ord(ch)
        # CJK Unified Ideographs (U+4E00–U+9FFF) + Extension A (U+3400–U+4DBF)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            return True
    return False


def _segment_text_with_readings(
    text: str, readings: dict[str, str]
) -> Iterator[str | Tuple[str, str]]:
    """Annotate text using pre-computed reading substrings.

    Match longest substrings first so compound keys (e.g. ``文庫本``)
    are preserved over their parts (``文庫``, ``本``).

    Keys that don't appear in *text* are silently ignored.

    No filtering is performed — callers are responsible for feeding
    clean data (LLM module filters its own output; katakana module
    produces clean English readings).
    """
    if not readings:
        yield text
        return

    _logger = logging.getLogger("epub_ruby")
    keys = sorted(readings.keys(), key=len, reverse=True)

    # Detect keys that don't appear in the text at all (debug only)
    unmatched = [k for k in keys if k not in text]
    if unmatched:
        _logger.debug(
            "%d key(s) not found in text (ignored): %s",
            len(unmatched),
            unmatched[:5]
        )

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
            yield from _split_tail(match, readings[match])
            i += len(match)
            continue

        buffer.append(text[i])
        i += 1

    if buffer:
        yield "".join(buffer)


def _generate_readings_from_cache(
    text: str,
    batch_readings: Dict[str, Dict[str, str]],
) -> Iterator[str | Tuple[str, str]]:
    """Like :func:`generate_readings` but uses pre-computed LLM batch readings.

    *batch_readings* contains LLM's readings for each sentence.
    Words the LLM did not annotate are left as plain text (no ruby).
    LLM mode and dictionary mode are independent – there is NO fallback
    to fugashi/dictionary annotation.

    Katakana loanwords receive English readings from local fugashi lookup
    (no LLM tokens consumed for this).

    If the LLM returned no readings for this sentence (missing key or empty
    dict), the sentence is yielded as plain text so processing can continue.
    An error is logged but processing is not interrupted.
    """
    llm_readings = batch_readings.get(text, {})

    # ── Build katakana→English dict independently ──────────────────
    from .katakana_english import extract_katakana_readings

    katakana_readings = extract_katakana_readings(text)

    # ── Merge: katakana (English) + LLM (hiragana) ──────────────────
    # The two dicts operate on disjoint domains — katakana words vs
    # kanji words — so overlapping keys indicate a bug in the LLM
    # module (LLM should never annotate pure-katakana words).
    conflicts = set(katakana_readings) & set(llm_readings)
    if conflicts:
        _logger = logging.getLogger("epub_ruby")
        _logger.warning(
            "Dict merge conflict! LLM tried to annotate katakana word(s): %s. "
            "Sentence: %s — keeping katakana reading, discarding LLM's.",
            sorted(conflicts), text[:80]
        )
        # Filter out conflicting keys from LLM readings — katakana dict
        # takes precedence.  Don't crash the entire EPUB just because
        # the LLM made a mistake on one sentence.
        llm_readings = {k: v for k, v in llm_readings.items()
                        if k not in conflicts}

    merged: dict[str, str] = {**katakana_readings, **llm_readings}

    if merged:
        yield from _segment_text_with_readings(text, merged)
        return

    # Neither LLM nor katakana produced any readings.
    # Yield the text as-is.
    _logger = logging.getLogger("epub_ruby")
    _logger.debug("No readings for sentence (keeping as plain text): %s", text[:80])
    yield text

class RubySoup:
    """Injects ``<ruby>`` annotations into BeautifulSoup-parsed HTML content.

    Args:
        is_ruby_rp: Whether to emit ``<rp>`` fallback parentheses.
        llm_reader: Optional :class:`LLMRubyReader` for context-aware kanji
            readings via LLM API.  When provided, the LLM annotates ALL
            kanji words purely from context (no dictionary hints).
            There is NO fugashi fallback — failed batches raise exceptions.
    """

    # Tags whose text content forms a single logical sentence / paragraph.
    # Text inside these elements is collected as one unit (joining across
    # inline tags like <b>, <i>, <span>) so the LLM gets full context.
    # Nested block-level elements are NOT recursed into – each block is
    # collected and processed independently.
    _BLOCK_TAGS: Set[str] = {
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "td", "th", "dt", "dd", "figcaption",
        "blockquote", "pre", "summary", "caption",
        "section", "article", "header", "footer", "aside", "nav",
    }

    # Tags that are "leaf" blocks — they cannot contain other block elements.
    # When a block element has nested block children (e.g. <div> wrapping
    # many <p> tags), we recurse into it rather than flattening everything.
    _LEAF_BLOCK_TAGS: Set[str] = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "td", "th", "dt", "dd", "figcaption",
        "pre", "summary", "caption",
    }

    @staticmethod
    def _has_block_children(element: Tag) -> bool:
        """Return True if *element* has any direct child that is a block tag
        (and not a ruby-related tag)."""
        for child in element.children:
            if isinstance(child, Tag):
                if child.name in ("ruby", "rt", "rp"):
                    continue
                if child.name in RubySoup._BLOCK_TAGS:
                    return True
        return False

    def __init__(
        self,
        is_ruby_rp: bool = True,
        llm_reader: "LLMRubyReader | None" = None,
    ) -> None:
        self._is_ruby_rp = is_ruby_rp
        self._llm_reader = llm_reader
        # Pre-computed LLM readings: {sentence: {word: reading}}
        self._batch_readings: Dict[str, Dict[str, str]] = {}
        # Count of sentences that received NO readings from LLM (for summary)
        self._skipped_count: int = 0

    def inject(self, soup: BeautifulSoup | Tag) -> None:
        """Recursively walk the soup tree and wrap text nodes with ``<ruby>`` tags.

        When using LLM, this does a TWO-PASS approach:
        1. Collect ALL text segments from the tree (at block level for context)
        2. Make batch API calls (LLM self-tokenizes and annotates substrings)
        3. Apply LLM readings to every text node
        """
        # --- Pass 1: collect all sentences for LLM self-tokenization ---
        if self._llm_reader is not None:
            items: List[str] = []
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

    @staticmethod
    def _get_block_text(element: Tag) -> str:
        """Extract text from a block-level element, joining across inline tags
        but NOT recursing into nested block elements.

        This gives the LLM full sentence context (e.g. "<b>漢字</b>を<b>勉強</b>"
        → "漢字を勉強") instead of fragmented pieces.

        Japanese text is joined WITHOUT spaces — adding spaces would
        alter the semantics and waste input tokens.
        """
        parts: List[str] = []
        for child in element.children:
            if child is None:
                continue
            if isinstance(child, NavigableString) and not isinstance(
                child, (Script, Stylesheet, TemplateString)
            ):
                parts.append(str(child))
            elif isinstance(child, Tag):
                if child.name in ("ruby", "rt", "rp"):
                    continue
                parts.append(child.get_text())
        # Collapse all whitespace (newlines, tabs, multiple spaces) into
        # nothing — Japanese doesn't use spaces as word separators.
        text = "".join(parts)
        return "".join(text.split())

    def _collect_segments(
        self, soup: BeautifulSoup | Tag,
        items: List[str],
    ) -> None:
        """Walk the tree and collect text segments for LLM annotation.

        Block-level elements have their FULL text collected as a single unit
        (joining across inline tags) so the LLM receives maximum context.
        Pure-kana blocks are skipped entirely.
        """
        for child in soup.children:
            if child is None:
                continue
            if isinstance(child, NavigableString) and not isinstance(
                child, (Script, Stylesheet, TemplateString)
            ):
                text = str(child).strip()
                if text and _contains_kanji(text):
                    items.append(text)
            elif isinstance(child, Tag):
                if child.name in ("ruby", "rt", "rp"):
                    continue
                if child.name in self._BLOCK_TAGS:
                    # If this block wraps nested blocks (e.g. <div>
                    # around many <p>), recurse so each inner block
                    # is collected individually.
                    if child.name not in self._LEAF_BLOCK_TAGS and \
                            self._has_block_children(child):
                        self._collect_segments(child, items)
                    else:
                        text = self._get_block_text(child)
                        if text and _contains_kanji(text):
                            items.append(text)
                else:
                    self._collect_segments(child, items)

    # ------------------------------------------------------------------
    # Pass 2: apply readings to text nodes
    # ------------------------------------------------------------------

    def _apply(self, soup: BeautifulSoup | Tag) -> None:
        """Walk tree and replace text nodes with <ruby> tags.

        Block-level elements are processed as a unit: the full text of the
        block is looked up in batch_readings, and the block's children are
        replaced with ruby-annotated content.  This preserves the context
        that was available to the LLM during annotation.
        """
        for child in soup.children:
            if child is None:
                continue
            if isinstance(child, NavigableString) and not isinstance(
                child, (Script, Stylesheet, TemplateString)
            ):
                # Text node NOT inside a block → process individually
                if child.strip():
                    self._process_text_node(child)
            elif isinstance(child, Tag):
                if child.name in ("ruby", "rt", "rp"):
                    continue
                if child.name in self._BLOCK_TAGS:
                    # If this block wraps nested blocks, recurse instead
                    # of flattening — consistent with _collect_segments.
                    if child.name not in self._LEAF_BLOCK_TAGS and \
                            self._has_block_children(child):
                        self._apply(child)
                    else:
                        self._apply_block(child)
                else:
                    # Inline / unknown tag: recurse
                    self._apply(child)

    def _apply_block(self, element: Tag) -> None:
        """Process a block-level element: look up the full text in
        batch_readings, build ruby-annotated replacement, and replace
        the element's children.
        """
        text = self._get_block_text(element)
        if not text:
            return

        # In LLM mode, skip blocks with no kanji — nothing to annotate
        if self._llm_reader is not None and not _contains_kanji(text):
            return

        base = _get_base_soup()

        if self._llm_reader is not None:
            segments = list(_generate_readings_from_cache(
                text, self._batch_readings
            ))
        else:
            segments = list(generate_readings(text))

        # Build replacement children
        new_children: list = []
        for seg in segments:
            if isinstance(seg, str):
                new_children.append(seg)
            elif isinstance(seg, tuple):
                text_part, reading = seg
                ruby_tag = base.new_tag("ruby")
                ruby_tag.append(text_part)
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
                new_children.append(ruby_tag)

        # Check how many segments are (text, reading) tuples vs plain strings
        # If ALL segments are plain strings (no ruby added) and we're in
        # LLM mode, increment the skipped counter for diagnostics.
        if self._llm_reader is not None:
            has_ruby = any(isinstance(s, tuple) for s in segments)
            if not has_ruby and segments:
                self._skipped_count += 1

        # Replace element children
        element.clear()
        for child in new_children:
            element.append(child)

    # ---- internal helpers --------------------------------------------------

    def _process_text_node(self, node: NavigableString) -> None:
        """Process a text node that is NOT inside a block-level element."""
        text = str(node).strip()
        if not text:
            return

        # In LLM mode: process full text as one unit (no whitespace split)
        # so it matches what was collected in _collect_segments.
        if self._llm_reader is not None:
            if not _contains_kanji(text):
                return
            base = _get_base_soup()
            wrapper = base.new_tag("temptag")
            for item in self._build_ruby_segments(text):
                wrapper.append(item)
            node.replace_with(wrapper)
            wrapper.unwrap()
            return

        # Dictionary mode: split by whitespace to preserve formatting
        base = _get_base_soup()
        wrapper = base.new_tag("temptag")
        for segment in _WHITESPACE_RE.split(text):
            if not segment.strip():
                wrapper.append(segment)
            else:
                for item in self._build_ruby_segments(segment):
                    wrapper.append(item)
        node.replace_with(wrapper)
        wrapper.unwrap()

    def _build_ruby_segments(self, text: str) -> Iterator[str | Tag]:
        if self._llm_reader is not None:
            # Use pre-computed LLM readings
            readings = _generate_readings_from_cache(text, self._batch_readings)
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
