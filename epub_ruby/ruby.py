from __future__ import annotations

import re
import logging
import unicodedata
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

    Matching uses NFKC comparison for robustness against NFD/NFC
    differences, but **output uses the original text characters**
    — no pos_map, no character loss.
    """
    if not readings:
        yield text
        return

    _logger = logging.getLogger("epub_ruby")

    # ── Build normalized-key lookup (longest-first) ─────────────
    norm_keys: list[tuple[str, str, int]] = []  # (norm_key, orig_key, orig_len)
    seen_norm: set[str] = set()
    for k in sorted(readings.keys(), key=len, reverse=True):
        nk = unicodedata.normalize("NFKC", k)
        if nk not in seen_norm:
            seen_norm.add(nk)
            norm_keys.append((nk, k, len(k)))

    # ── Walk ORIGINAL text, matching keys via NFKC comparison ──
    buffer: list[str] = []
    i = 0
    tlen = len(text)
    while i < tlen:
        match_orig_key: str | None = None
        match_len: int = 0
        for nk, orig_key, kl in norm_keys:
            if i + kl > tlen:
                continue
            # Compare NFKC-normalized slices for robust matching
            if unicodedata.normalize("NFKC", text[i:i + kl]) == nk:
                match_orig_key = orig_key
                match_len = kl
                break

        if match_orig_key is not None:
            if buffer:
                yield "".join(buffer)
                buffer = []
            yield from _split_tail(match_orig_key, readings[match_orig_key])
            i += match_len
            continue

        # No match: consume one original character as plain text
        buffer.append(text[i])
        i += 1

    if buffer:
        yield "".join(buffer)


def _generate_readings_from_cache(
    text: str,
    batch_readings: Dict[str, Dict[str, str]],
    batch_readings_norm: Dict[str, Dict[str, str]] | None = None,
) -> Iterator[str | Tuple[str, str]]:
    """Like :func:`generate_readings` but uses pre-computed LLM batch readings.

    *batch_readings* contains LLM's readings for each sentence.
    Words the LLM did not annotate are left as plain text (no ruby).
    LLM mode and dictionary mode are independent – there is NO fallback
    to fugashi/dictionary annotation.

    Katakana loanwords receive English readings from local fugashi lookup
    (no LLM tokens consumed for this).

    If *batch_readings_norm* is provided and the exact *text* is not found
    in *batch_readings*, the normalized form of *text* is used as a
    fallback key into *batch_readings_norm*.  This guards against
    Unicode-representation mismatches and dedup artifacts.

    If the LLM returned no readings for this sentence (missing key or empty
    dict), the sentence is yielded as plain text so processing can continue.
    An error is logged but processing is not interrupted.
    """
    llm_readings = batch_readings.get(text, {})

    # ── Normalized fallback ──────────────────────────────────────
    if not llm_readings and batch_readings_norm is not None:
        from .llm_ruby import _normalize_sentence
        norm_key = _normalize_sentence(text)
        llm_readings = batch_readings_norm.get(norm_key, {})

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
        # ── Verify annotation completeness ─────────────────────────
        # After segmentation, check if any kanji remains in the plain
        # text output — those are words the LLM missed.
        segments = list(_segment_text_with_readings(text, merged))
        missed_kanji_chars: list[str] = []
        for seg in segments:
            if isinstance(seg, str) and _contains_kanji(seg):
                missed_kanji_chars.append(seg)
        if missed_kanji_chars:
            _logger = logging.getLogger("epub_ruby")
            _logger.error(
                "PARTIAL ANNOTATION: %d kanji segment(s) not covered by "
                "LLM readings in sentence: %s",
                len(missed_kanji_chars), text[:80]
            )
            # Don't raise here — let _verify_kanji_coverage catch it.
            # The segments are yielded as-is (plain text for missed kanji).
        yield from segments
        return

    # Neither LLM nor katakana produced any readings.
    # This is a HARD ERROR for sentences that contain kanji —
    # the LLM failed to annotate a sentence that needs furigana.
    # Raise an exception so the caller can abort instead of silently
    # producing incomplete output.
    _logger = logging.getLogger("epub_ruby")
    if _contains_kanji(text):
        _logger.error(
            "MISSED ANNOTATION: kanji sentence got ZERO readings from LLM. "
            "Sentence: %s", text[:120]
        )
        # Don't raise — _verify_kanji_coverage catches this.
    else:
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
        # Normalized-key lookup (built lazily for Unicode-robust fallback)
        self._batch_readings_norm: Dict[str, Dict[str, str]] | None = None
        # Count of sentences that received NO readings from LLM (for summary)
        self._skipped_count: int = 0

    def _get_batch_readings_norm(self) -> Dict[str, Dict[str, str]]:
        """Build and return a normalized-key → readings lookup dict.

        This is built lazily from ``_batch_readings`` so that
        ``_generate_readings_from_cache`` can fall back to normalized
        text matching when the exact text key is not found (Unicode
        representation mismatches, dedup artifacts, etc.).
        """
        if self._batch_readings_norm is None:
            from .llm_ruby import _normalize_sentence
            norm: Dict[str, Dict[str, str]] = {}
            for key, val in self._batch_readings.items():
                nk = _normalize_sentence(key)
                if nk not in norm:
                    norm[nk] = val
            self._batch_readings_norm = norm
        return self._batch_readings_norm

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

        Includes a **text integrity check**: after building the annotated
        replacement, the plain-text content is verified to match the
        original.  If verification fails, the element is LEFT UNCHANGED
        to prevent text corruption.
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
                text, self._batch_readings,
                batch_readings_norm=self._get_batch_readings_norm(),
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

        # ── TEXT INTEGRITY CHECK ──────────────────────────────────
        # Reconstruct the plain text from new_children and verify it
        # matches the original block text.  If not, the annotation
        # logic has a bug and we MUST NOT corrupt the output.
        rebuilt_parts: list[str] = []
        for child in new_children:
            if isinstance(child, str):
                rebuilt_parts.append(child)
            elif isinstance(child, Tag):
                # Extract only the BASE text of ruby tags (first child is
                # the base text; rt/rp content is reading/fallback)
                if child.name == "ruby":
                    base_text = "".join(
                        str(c) for c in child.children
                        if not (isinstance(c, Tag) and c.name in ("rt", "rp"))
                    )
                    rebuilt_parts.append(base_text)
                else:
                    rebuilt_parts.append(child.get_text())
        rebuilt_text = "".join(rebuilt_parts)

        if rebuilt_text != text:
            _logger = logging.getLogger("epub_ruby")
            _logger.error(
                "TEXT INTEGRITY CHECK FAILED for block <%s>! "
                "Original (%d chars): %r  |  Rebuilt (%d chars): %r. "
                "Keeping original text unchanged to prevent corruption.",
                element.name, len(text), text[:200],
                len(rebuilt_text), rebuilt_text[:200],
            )
            # DO NOT modify the element — leave it as-is
            self._skipped_count += 1
            import sys
            rd = self._batch_readings.get(text, {})
            print(
                f"  [SKIP-INTEGRITY #{self._skipped_count}] block <{element.name}> "
                f"orig_len={len(text)} rebuilt_len={len(rebuilt_text)} "
                f"orig[:80]={text[:80]!r} rebuilt[:80]={rebuilt_text[:80]!r} "
                f"reading_keys={list(rd.keys())[:5]} "
                f"n_segments={len(segments)}",
                file=sys.stderr, flush=True
            )
            return

        # ── Integrity passed: apply the annotation ───────────────

        # Check how many segments are (text, reading) tuples vs plain strings
        # If ALL segments are plain strings (no ruby added) and we're in
        # LLM mode, increment the skipped counter for diagnostics.
        if self._llm_reader is not None:
            has_ruby = any(isinstance(s, tuple) for s in segments)
            if not has_ruby and segments:
                self._skipped_count += 1
                import sys
                # Show what readings exist for this sentence so we
                # can diagnose why segmentation produced no ruby.
                rd = self._batch_readings.get(text, {})
                rd_keys = list(rd.keys())[:5] if rd else []
                print(
                    f"  [SKIP #{self._skipped_count}] block <{element.name}> "
                    f"no ruby: text[:80]={text[:80]!r}  "
                    f"in_batch={text in self._batch_readings}  "
                    f"has_kanji={_contains_kanji(text)}  "
                    f"reading_keys={rd_keys}",
                    file=sys.stderr, flush=True
                )

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

            # Build segments
            segments = []
            for item in self._build_ruby_segments(text):
                segments.append(item)

            # ── Integrity check ────────────────────────────────
            rebuilt_parts = []
            for seg in segments:
                if isinstance(seg, str):
                    rebuilt_parts.append(seg)
                elif isinstance(seg, Tag):
                    if seg.name == "ruby":
                        base_text = "".join(
                            str(c) for c in seg.children
                            if not (isinstance(c, Tag) and c.name in ("rt", "rp"))
                        )
                        rebuilt_parts.append(base_text)
                    else:
                        rebuilt_parts.append(seg.get_text())
            rebuilt = "".join(rebuilt_parts)
            if rebuilt != text:
                _logger = logging.getLogger("epub_ruby")
                _logger.error(
                    "TEXT INTEGRITY CHECK FAILED for text node! "
                    "Original (%d chars): %r  |  Rebuilt (%d chars): %r. "
                    "Keeping original text unchanged to prevent corruption.",
                    len(text), text[:200], len(rebuilt), rebuilt[:200],
                )
                self._skipped_count += 1
                import sys
                rd = self._batch_readings.get(text, {})
                print(
                    f"  [SKIP-INTEGRITY-NODE #{self._skipped_count}] "
                    f"orig_len={len(text)} rebuilt_len={len(rebuilt)} "
                    f"orig[:80]={text[:80]!r} rebuilt[:80]={rebuilt[:80]!r} "
                    f"reading_keys={list(rd.keys())[:5]}",
                    file=sys.stderr, flush=True
                )
                return  # leave node unchanged

            wrapper = base.new_tag("temptag")
            for item in segments:
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
            readings = _generate_readings_from_cache(
                text, self._batch_readings,
                batch_readings_norm=self._get_batch_readings_norm(),
            )
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
