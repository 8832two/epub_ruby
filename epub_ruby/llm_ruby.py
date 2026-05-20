from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import threading
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from .api_pool import APIPool, APIConfig, APIPoolExhaustedError, APITruncatedError
from .exceptions import LLMAPIError, LLMBatchError

_logger = logging.getLogger("epub_ruby")

# 磁盘缓存目录
_CACHE_DIR = Path.home() / ".epub_ruby" / "llm_cache"
_CACHE_FILE = _CACHE_DIR / "readings.json"


# ---------------------------------------------------------------------------
# Kanji detection (fast, no fugashi dependency)
# ---------------------------------------------------------------------------

def _contains_kanji(text: str) -> bool:
    """Return ``True`` if *text* contains at least one CJK unified ideograph."""
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            return True
    return False


def _extract_kanji_words(text: str) -> list[str]:
    """Extract all contiguous CJK-kanji runs from *text*.

    For ``"私は毎日日本語を勉強します"`` returns
    ``["私", "毎日", "日本語", "勉強"]``.

    Used to detect **partial** annotation misses — when the LLM annotates
    *some* kanji words in a sentence but silently skips others.
    """
    words: list[str] = []
    current: list[str] = []
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            current.append(ch)
        else:
            if current:
                words.append("".join(current))
                current = []
    if current:
        words.append("".join(current))
    return words


def _is_kanji_covered(kanji_word: str, covered_keys: set[str]) -> bool:
    """Check if *kanji_word* is covered by any LLM annotation key.

    Uses **bidirectional** substring containment:

    - ``kanji_word in key`` — LLM key includes okurigana
      (e.g. ``"食" in "食べる"``, ``"話" in "話して"``)
    - ``key in kanji_word`` — LLM split a compound into sub-words
      (e.g. ``"去年" in "去年退学"``, ``"唯一" in "唯一普通"``)

    Both *kanji_word* and keys are NFKC-normalized before comparison
    to handle Unicode representation differences.
    """
    kw_norm = unicodedata.normalize("NFKC", kanji_word)
    for key in covered_keys:
        key_norm = unicodedata.normalize("NFKC", key)
        if kw_norm in key_norm or key_norm in kw_norm:
            return True
    return False


def _readings_match_sentence(sentence: str, readings: dict[str, str]) -> float:
    """Return the fraction of *readings* keys that appear in *sentence*.

    Used to detect **positional shift**: when the LLM returns items in
    wrong order, the keys won't appear in the corresponding sentence.

    Returns 1.0 if all keys are found, 0.0 if none are.
    Empty *readings* returns 1.0 (no mismatch to detect).

    Uses NFKC normalization so that different Unicode representation
    forms of the same character (NFC vs NFD) still match.
    """
    if not readings:
        return 1.0
    sent_norm = unicodedata.normalize("NFKC", sentence)
    matching = sum(1 for k in readings if unicodedata.normalize("NFKC", k) in sent_norm)
    return matching / len(readings)


def _kanji_sequence_in_text(key: str, text: str) -> bool:
    """Check if the kanji characters in *key* appear in *text* in order.

    Unlike simple substring matching, this handles okurigana conjugation
    mismatches.  For example, the LLM may output ``"合わせる"`` (dictionary
    form) but the sentence contains ``"合わせてる"`` (conjugated form).
    Simple ``in`` check fails, but the kanji sequence ``["合"]`` still
    matches ``"合わせてる"``.

    Both *key* and *text* are NFKC-normalized before comparison.
    """
    key_norm = unicodedata.normalize("NFKC", key)
    text_norm = unicodedata.normalize("NFKC", text)

    # ── Fast path: exact substring match ──────────────────────────
    if key_norm in text_norm:
        return True

    # ── Extract kanji from key ────────────────────────────────────
    key_kanji = [ch for ch in key_norm if _contains_kanji(ch)]
    if not key_kanji:
        return False  # no kanji → use substring match only

    # ── Check if kanji sequence appears in text in order ──────────
    ki = 0
    for ch in text_norm:
        if ch == key_kanji[ki]:
            ki += 1
            if ki == len(key_kanji):
                return True
    return False


def _normalize_sentence(text: str) -> str:
    """Normalize sentence for cache key: collapse whitespace, strip.
    
    Japanese text doesn't use spaces between words, so collapsing
    whitespace doesn't change meaning but greatly improves cache hit rate.
    """
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Reading quality validation & sanitization
# ---------------------------------------------------------------------------

# Regex: hiragana-only (including standard punctuation like ー for long vowels)
_HIRAGANA_RE = re.compile(r"^[ぁ-ゖー\s]+$")
# Regex: katakana-only
_KATAKANA_RE = re.compile(r"^[ァ-ヶー\s]+$")
# Regex: contains any forbidden characters in readings (=, numbers, latin, kanji)
_FORBIDDEN_IN_READING_RE = re.compile(r"[=＝0-9０-９a-zA-Zａ-ｚＡ-Ｚ\u4E00-\u9FFF\u3400-\u4DBF]")


def _is_valid_reading(reading: str) -> bool:
    """Check if a reading string is valid (pure kana, no garbage).

    A valid reading:
      - Is non-empty
      - Contains ONLY hiragana, katakana, or long-vowel mark (ー)
      - Does NOT contain kanji, latin letters, numbers, equals signs
    """
    if not reading or not reading.strip():
        return False
    reading = reading.strip()
    if _FORBIDDEN_IN_READING_RE.search(reading):
        return False
    return True


def _sanitize_reading(raw: str) -> str:
    """Clean up a raw LLM reading string.

    Handles common LLM mistakes:
      - "じどうてきなんだよ=なんだよ" → "じどうてきなんだよ"
      - "しゅういに=に" → "しゅういに"
      - "わたし (I)" → "わたし"
      - Extra spaces, punctuation, etc.
    """
    if not raw:
        return ""
    raw = raw.strip()
    # Chop at first = or ＝ (LLM sometimes adds explanation after =)
    eq_pos = raw.find("=")
    if eq_pos == -1:
        eq_pos = raw.find("＝")
    if eq_pos > 0:
        raw = raw[:eq_pos].strip()
    # Chop at first ( or （ (LLM sometimes adds parenthetical notes)
    paren_pos = raw.find("(")
    if paren_pos == -1:
        paren_pos = raw.find("（")
    if paren_pos > 0:
        raw = raw[:paren_pos].strip()
    # Chop at first space if the reading is mostly kana
    space_pos = raw.find(" ")
    if space_pos > 0:
        before = raw[:space_pos]
        if _HIRAGANA_RE.match(before) or _KATAKANA_RE.match(before):
            raw = before
    return raw


def _sanitize_readings_dict(readings: dict[str, str]) -> dict[str, str]:
    """Clean and validate a dict of {word: reading} from LLM output.

    Returns a new dict with only valid entries.
    Removes:
      - Entries where the key doesn't contain kanji (pure-kana annotation)
      - Entries where the reading equals the key (no-op)
      - Entries with invalid/garbage readings
    """
    cleaned: dict[str, str] = {}
    for key, raw_reading in readings.items():
        if not isinstance(key, str) or not key.strip():
            continue
        key = key.strip()
        # Skip pure-kana keys — nothing to annotate
        if not _contains_kanji(key):
            continue
        # Skip keys that are too long (likely hallucinated)
        if len(key) > 30:
            continue
        # Sanitize the reading
        reading = _sanitize_reading(str(raw_reading)) if raw_reading else ""
        if not reading:
            continue
        # Skip if reading equals key (no-op annotation)
        if reading == key:
            continue
        # Validate reading is pure kana
        if not _is_valid_reading(reading):
            continue
        cleaned[key] = reading
    return cleaned

# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

_JSON_PATTERN = re.compile(r"\{[^{}]*\}", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def _extract_json(text: str) -> Dict[str, Any]:
    """Try to pull a JSON object out of an LLM response string.

    Handles:
      - Markdown code fences (```` ```json ... ``` ````)
      - Nested braces in JSON values
      - Plain JSON
    """
    text = text.strip()

    # 1. Strip markdown code fences
    m = _CODE_FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()

    # 2. Try direct parse
    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        pass

    # 3. Brace-matching extraction (handles nested braces)
    start = text.find("{")
    if start == -1:
        return {}

    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)  # type: ignore[no-any-return]
                except json.JSONDecodeError:
                    # ── Try to repair common truncation issues ──
                    repaired = _repair_truncated_json(candidate)
                    if repaired is not None:
                        try:
                            return json.loads(repaired)  # type: ignore[no-any-return]
                        except json.JSONDecodeError:
                            pass
                    return {}
    return {}


def _repair_truncated_json(text: str) -> str | None:
    """Try to fix a truncated JSON object string.

    Handles common LLM output truncation:
      - Unterminated string value: ``{"key":"val}`` → ``{"key":"val"}``
      - Missing closing brace: ``{"key":"val"`` → ``{"key":"val"}``

    Returns the repaired string, or ``None`` if repair is impossible.
    """
    if not text or not (text.startswith("{") and text.endswith("}")):
        return None

    inner = text[1:-1]  # content between { and }

    # ── Count quotes to detect unterminated strings ─────────
    # In valid JSON like {"k":"v"}, there are always an EVEN number
    # of unescaped double-quotes.  An odd count means a string is
    # unterminated.
    quote_count = 0
    escape = False
    for ch in inner:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            quote_count += 1

    if quote_count % 2 != 0:
        # Odd quotes → a string value is unterminated.
        # Append a closing quote just before the final }.
        return text[:-1] + '"' + text[-1]

    # ── No closing brace at all ──────────────────────────
    if not text.endswith("}"):
        # Check if text looks like valid JSON except missing }
        if quote_count % 2 == 0 and text.endswith('"'):
            return text + "}"

    return None


def _salvage_partial_json(
    content: str, expected_count: int
) -> tuple[list[dict[str, str]] | None, int]:
    """Extract as many complete array items as possible from truncated JSON.

    When the LLM response is cut off mid-stream, the JSON is invalid as a
    whole, but individual array items may be complete and parseable.
    This function walks the ``"r"`` array character-by-character to find
    complete ``{...}`` objects that can be salvaged.

    Returns:
        ``(salvaged_items, salvaged_count)`` where *salvaged_items* is
        ``None`` if no items could be salvaged, otherwise a list of
        ``{word: reading}`` dicts.  *salvaged_count* is always the number
        of complete items found (0 if none).
    """
    if not content:
        return None, 0
    text = content.strip()

    # Find the "r" array start: "r": [
    import re as _re
    match = _re.search(r'"r"\s*:\s*\[', text)
    if not match:
        return None, 0
    pos = match.end()

    items: list[dict[str, str]] = []
    depth = 0
    item_start = -1
    in_string = False
    escape = False
    n = len(text)

    while pos < n:
        ch = text[pos]
        if escape:
            escape = False
            pos += 1
            continue
        if ch == '\\':
            escape = True
            pos += 1
            continue
        if ch == '"':
            in_string = not in_string
            pos += 1
            continue
        if in_string:
            pos += 1
            continue

        # Outside strings — track braces
        if ch == '{':
            if depth == 0:
                item_start = pos
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and item_start >= 0:
                # Complete item found
                try:
                    item = json.loads(text[item_start:pos + 1])
                    if isinstance(item, dict):
                        # Convert all values to str
                        clean: dict[str, str] = {}
                        for k, v in item.items():
                            if isinstance(k, str) and k.strip():
                                clean[k.strip()] = str(v) if isinstance(v, str) else str(v)
                        if clean:
                            items.append(clean)
                        else:
                            items.append({})
                    else:
                        items.append({})
                except json.JSONDecodeError:
                    items.append({})
                item_start = -1
        elif ch == ']' and depth == 0:
            # Array closed naturally — we shouldn't reach here normally
            break

        pos += 1

    salvaged = len(items)
    if salvaged == 0:
        return None, 0
    return items, salvaged


def _looks_truncated(content: str) -> bool:
    """Heuristic: does *content* look like truncated/incomplete JSON?

    Checks for:
      - Unbalanced braces/brackets (more ``{`` than ``}``)
      - Ends mid-string (trailing ``"`` without value completion)
      - Ends with a comma or colon (incomplete JSON value)
    """
    if not content:
        return False
    stripped = content.strip()

    # ── Unbalanced array brackets ─────────────────────────────
    open_braces = stripped.count("{")
    close_braces = stripped.count("}")
    open_brackets = stripped.count("[")
    close_brackets = stripped.count("]")
    if open_braces != close_braces:
        return True
    if open_brackets != close_brackets:
        return True

    # ── Ends mid-value ────────────────────────────────────────
    last_non_ws = stripped.rstrip()[-1:] if stripped.rstrip() else ""
    if last_non_ws in (",", ":", '"'):
        return True

    # ── Abrupt end: content doesn't end with ] or } ───────────
    if last_non_ws not in ("}", "]", '"'):
        return True

    return False


# ---------------------------------------------------------------------------
# LLMRubyReader
# ---------------------------------------------------------------------------


class LLMRubyReader:
    """Query LLM APIs for context-aware kanji readings.

    Supports multiple API providers with automatic failover via :class:`APIPool`.
    Features persistent disk cache so re-processing the same book costs ZERO tokens.

    Usage::

        # Single provider (backward-compatible)
        reader = LLMRubyReader(api_key="sk-...", model="deepseek-v4-flash")

        # Multiple providers with failover
        pool = APIPool([
            APIConfig(provider="deepseek", api_key="sk-...", model="deepseek-v4-flash"),
            APIConfig(provider="gemini", api_key="...", model="gemini-3-flash-preview"),
        ])
        reader = LLMRubyReader(pool=pool)

        readings = reader.get_readings_batch(["私は毎日日本語を勉強します"])
        # → {"私は毎日日本語を勉強します": {"私": "わたし", "毎日": "まいにち", ...}}
    """

    # ── System prompts ──────────────────────────────────────────────────
    # The PRIMARY prompt uses JSON output with response_format enforcement.
    # The RETRY prompt is used when the LLM goes off-task (translation,
    # analysis, etc.) — it escalates the warning.

    _SYSTEM_PROMPT_BATCH = """\
You are a kanji→hiragana converter. Output ONLY a JSON object.

Below are numbered Japanese sentences. Find EVERY kanji word in EVERY sentence and give its hiragana reading. Do NOT skip any kanji word.

Output format: {"r":[{...},{...},...]}
- The array contains kanji→hiragana dicts
- Each key is a kanji word copied EXACTLY from the input (NOT "kanji"/"word")
- Each value is PURE HIRAGANA only: no "=", "()", spaces, notes
- Put ALL readings into one dict, or spread across several — either is fine

EXAMPLES:
Input:
[1] 私は毎日日本語を勉強します
[2] こんにちは
[3] 彼女は走った
Output:
{"r":[{"私":"わたし","毎日":"まいにち","日本語":"にほんご","勉強":"べんきょう"},{},{"彼女":"かのじょ","走":"はし"}]}

Input:
[1] 紫色の煙が立ちのぼる
Output:
{"r":[{"紫色":"むらさきいろ","煙":"けむり","立":"た"}]}

Output ONLY the JSON. No other text."""

    _SYSTEM_PROMPT_BATCH_RETRY = """\
PREVIOUS OUTPUT WAS INVALID. You MUST output ONLY a JSON object.

Format: {"r":[{"actual_kanji":"hiragana",...},{...},...]}

CRITICAL:
- Each key MUST be the actual kanji word copied from the input — NOT "kanji"/"word"
- Each value MUST be PURE HIRAGANA only — no "=", "()", spaces
- Find EVERY kanji word in EVERY input line — do NOT skip any
- Put all readings in one dict or spread across several — either is fine

CORRECT:
Input:
[1] 私は毎日日本語を勉強します
[2] こんにちは
Output:
{"r":[{"私":"わたし","毎日":"まいにち","日本語":"にほんご","勉強":"べんきょう"},{}]}

Output ONLY the JSON object. Nothing else."""

    # ── Off-task detection ───────────────────────────────────────────
    # When the LLM ignores instructions and outputs translations or
    # analysis, these patterns help detect it so we can retry with
    # an escalated prompt rather than silently failing.

    _OFF_TASK_PATTERNS: tuple[str, ...] = (
        # Chinese characters (simplified & traditional) — translation
        "翻译", "分析", "总结", "评论", "概括", "剧情", "角色",
        "场景", "对话", "心理", "描写", "设定", "人物", "故事",
        "小说", "轻小说", "系列", "作品", "作者", "主人公",
        "叙述", "叙述者", "视角", "第一人称", "主题", "情节",
        "发展", "后续", "续写", "改编", "结局",
        "翻譯", "場景", "對話", "視角", "後續", "結局",
        # English — analysis
        "analysis", "summary", "translation", "character",
        "chapter", "episode", "novel", "scene", "story",
        # Japanese phrases that indicate analysis
        "主人公", "登場人物", "あらすじ", "ストーリー",
        # Common off-task starters
        "当然", "好的", "以下是", "这段", "这是一", "您提供",
        "I'd be happy", "Here is", "This is a",
        "Sure", "Certainly",
    )

    @classmethod
    def _is_off_task_response(cls, text: str) -> bool:
        """Return ``True`` if *text* looks like translation/analysis, not JSON."""
        stripped = text.strip()

        # ── JSON-structured text can still be off-task ──────────────
        # If it starts with { or [, try to parse it and check if the
        # content looks like regurgitated input (empty keys, long
        # sentence values, etc.) rather than actual readings.
        if stripped.startswith("{") or stripped.startswith("["):
            # Parse and inspect content quality
            try:
                data = _extract_json(stripped)
                if isinstance(data, dict):
                    raw = data.get("r") or data.get("results")
                    if isinstance(raw, list) and len(raw) > 0:
                        empty_key_items = 0
                        long_value_items = 0
                        placeholder_key_items = 0
                        _PLACEHOLDER_KEYS = {"kanji", "word", "reading", "term", "text", "hiragana", "i", "#"}
                        for item in raw:
                            if isinstance(item, dict):
                                all_empty = True
                                has_long_value = False
                                all_placeholder = True
                                for k, v in item.items():
                                    if isinstance(k, str) and k.strip():
                                        all_empty = False
                                        if k.strip().lower() not in _PLACEHOLDER_KEYS:
                                            all_placeholder = False
                                    if isinstance(v, str) and len(v) > 30:
                                        has_long_value = True
                                if all_empty and len(item) > 0:
                                    empty_key_items += 1
                                if has_long_value:
                                    long_value_items += 1
                                if all_placeholder and len(item) > 0:
                                    placeholder_key_items += 1
                        # If most items have empty keys → regurgitated input
                        if empty_key_items > len(raw) * 0.3:
                            return True
                        # If most values are very long sentences → off-task
                        if long_value_items > len(raw) * 0.5:
                            return True
                        # If most items use placeholder keys ("kanji"/"word")
                        if placeholder_key_items > len(raw) * 0.3:
                            return True
                return False  # JSON looks valid, not off-task
            except Exception:
                pass  # fall through to text-based detection

        # Check for markdown code fences with JSON inside
        if stripped.startswith("```"):
            return False

        # Check for off-task patterns
        lower = stripped.lower()
        for pattern in cls._OFF_TASK_PATTERNS:
            if pattern.lower() in lower:
                return True

        # Heuristic: if the text is very long (>500 chars) and contains
        # natural language sentences (periods, newlines as paragraph breaks),
        # it's probably off-task.
        if len(stripped) > 500:
            # Count sentence-ending punctuation
            sentence_ends = stripped.count("。") + stripped.count("！") + stripped.count("？") + stripped.count(".")
            if sentence_ends >= 2:
                return True

        return False

    @staticmethod
    def _validate_batch_response(
        data: Any, item_count: int,
        item_has_kanji: list[bool] | None = None,
        missed_kanji_indices: list[int] | None = None,
        skip_quality_gates: bool = False,
    ) -> list[dict[str, str]] | None:
        """Validate AND sanitize the parsed JSON response structure.

        Returns a list of ``{word: reading}`` dicts (length = *item_count*)
        with only valid, clean readings — or ``None`` if the structure is
        fundamentally invalid.

        If *item_has_kanji* and *missed_kanji_indices* are provided, indices
        of kanji-containing items that received ZERO annotations are appended
        to *missed_kanji_indices*.

        If *skip_quality_gates* is True (used for salvaged/truncated data),
        the aggregate quality thresholds are skipped — individual items are
        still sanitized but the batch is never rejected for having too many
        empty items.

        Each per-line dict is run through :func:`_sanitize_readings_dict`
        to strip:
          - The "i" / "#" metadata keys (sentence index markers)
          - "=" and trailing garbage (e.g. "しゅうい=に" → "しゅうい")
          - Parenthetical notes (e.g. "わたし (I)" → "わたし")
          - Pure-kana keys (nothing to annotate)
          - Non-hiragana readings
          - No-op annotations (reading == key)
        """
        if not isinstance(data, dict):
            return None

        # Support both {"r": [...]} and {"results": [...]} formats
        raw = data.get("r") or data.get("results")
        if not isinstance(raw, list):
            return None

        # ── Extract "i" values BEFORE sanitization (for position mapping) ──
        # The "i" key is stripped by _sanitize_readings_dict (not kanji),
        # but we need it preserved for the caller to do position remapping.
        # We store it in a separate list and strip it explicitly here.
        raw_positions: list[int | None] = []  # None = no "i" key
        for item in raw:
            if isinstance(item, dict):
                i_val = item.get("i")
                if i_val is not None:
                    try:
                        raw_positions.append(int(i_val) - 1)  # 0-based
                    except (ValueError, TypeError):
                        raw_positions.append(None)
                else:
                    raw_positions.append(None)
            else:
                raw_positions.append(None)

        # Pad or truncate to match item_count
        result: list[dict[str, str]] = []
        empty_key_count = 0   # items where ALL keys are empty strings
        useless_item_count = 0  # items that had raw entries but all were filtered out
        placeholder_key_count = 0  # items using literal "kanji"/"word" as key (LLM confused)
        _PLACEHOLDER_KEYS = {"kanji", "word", "reading", "term", "text", "hiragana", "i", "#"}
        for i in range(item_count):
            if i < len(raw):
                item = raw[i]
                if not isinstance(item, dict):
                    result.append({})
                    continue
                # Collect raw entries, then sanitize
                raw_dict: dict[str, str] = {}
                had_empty_keys = False
                had_raw_entries = False
                all_keys_placeholder = True
                for k, v in item.items():
                    if isinstance(k, str) and isinstance(v, str):
                        raw_dict[k] = v
                        had_raw_entries = True
                        if k.strip() == "":
                            had_empty_keys = True
                        if k.strip().lower() not in _PLACEHOLDER_KEYS:
                            all_keys_placeholder = False
                    elif isinstance(k, str) and not isinstance(v, str):
                        raw_dict[k] = str(v)
                        had_raw_entries = True
                        if k.strip() == "":
                            had_empty_keys = True
                        if k.strip().lower() not in _PLACEHOLDER_KEYS:
                            all_keys_placeholder = False
                # Apply full sanitization pipeline
                sanitized = _sanitize_readings_dict(raw_dict)
                result.append(sanitized)
                # Track items that had content but became empty after sanitization
                if had_raw_entries and not sanitized:
                    useless_item_count += 1
                # Track items where ALL non-empty keys were absent
                if had_empty_keys and not sanitized:
                    empty_key_count += 1
                # Track items using placeholder keys (LLM confused about format)
                if had_raw_entries and all_keys_placeholder and not sanitized:
                    placeholder_key_count += 1
            else:
                result.append({})

        # ── Kanji-aware missed-annotation detection ──────────────────
        # If item_has_kanji is provided, track which kanji items got
        # ZERO annotations (LLM returned {} or all entries were filtered).
        if item_has_kanji is not None and missed_kanji_indices is not None:
            for i in range(min(item_count, len(item_has_kanji))):
                if item_has_kanji[i] and not result[i]:
                    missed_kanji_indices.append(i)

        # ── Quality gate: reject responses that are structurally valid
        #     but semantically empty (LLM outputting empty keys, regurgitated
        #     sentences, etc.) ──────────────────────────────────────────
        # Skip when salvaging truncated data — we expect trailing empties.
        if not skip_quality_gates:
            total_raw_items = min(len(raw), item_count)
            if total_raw_items > 0:
                # If > 20% of items have empty keys OR > 40% of items are
                # useless (had content but all filtered), treat as invalid
                if empty_key_count > total_raw_items * 0.2:
                    return None
                if useless_item_count > total_raw_items * 0.4:
                    return None
                # If > 20% use placeholder keys ("kanji" instead of actual word)
                # — lowered from 30% because the retry prompt now has
                #   concrete examples so placeholder keys indicate a
                #   fundamental misunderstanding by the LLM.
                if placeholder_key_count > total_raw_items * 0.2:
                    return None
                # If > 30% of kanji items got ZERO annotations, treat as invalid
                # (lowered from 50% — 30% zero is already a serious problem)
                if item_has_kanji is not None and missed_kanji_indices is not None:
                    kanji_total = sum(1 for b in item_has_kanji[:item_count] if b)
                    if kanji_total > 0:
                        kanji_missed = sum(
                            1 for idx in missed_kanji_indices
                            if idx < item_count and idx < len(item_has_kanji) and item_has_kanji[idx]
                        )
                        if kanji_missed > kanji_total * 0.3:
                            return None

        return result

    def __init__(
        self,
        # -- New API: pass an APIPool directly --
        pool: Optional[APIPool] = None,
        # -- Legacy API (backward-compatible) --
        api_key: Optional[str] = None,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        # -- Common settings --
        batch_size: int = 200,
        cache_size: int = 16384,
        rate_limit_delay: float = 0.1,
        timeout: float = 120.0,
        max_retries: int = 2,
        max_concurrent: int = 0,
        max_tokens: int = 384000,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Create a new LLM-based ruby reader.

        Args:
            pool: An :class:`APIPool` instance for multi-provider support.
                When provided, *api_key*, *model*, *base_url* are ignored.
            api_key: API key (legacy, single provider).
            model: Model name.
            base_url: API base URL (legacy).
            batch_size: Max sentences per API call (default 200).
                Larger = fewer calls = fewer tokens wasted on prompts.
            cache_size: Max in-memory cache entries.
            rate_limit_delay: Seconds between API calls.
            timeout: HTTP request timeout in seconds.
            max_retries: Max retries per failed API call.
            max_concurrent: Max simultaneous API calls (0=unlimited).
            max_tokens: Max output tokens per API call (default 16384).
                Increase for large batch sizes to prevent truncation.
            progress_callback: Optional fn(completed, total) per chunk.
        """
        if pool is not None:
            self._pool = pool
            self._model = ""  # let pool use each provider's own model
        else:
            key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
            if not key:
                raise ValueError(
                    "API key not found. Set DEEPSEEK_API_KEY environment "
                    "variable, pass api_key=..., or provide an APIPool."
                )
            self._pool = APIPool([
                APIConfig(
                    provider="deepseek",
                    api_key=key,
                    model=model,
                    base_url=base_url,
                )
            ], timeout=timeout)
            self._model = model

        self._batch_size = batch_size
        self._rate_limit_delay = rate_limit_delay
        self._last_call_time = 0.0
        self._max_retries = max_retries
        self._max_tokens = max_tokens
        self._lock = threading.Lock()
        self._progress_callback = progress_callback

        # Concurrency control
        self._max_concurrent = max_concurrent
        self._concurrency_sem: threading.Semaphore | None = (
            threading.Semaphore(max_concurrent) if max_concurrent > 0 else None
        )

        # ── Two-tier cache: in-memory (fast) + disk (persistent) ──
        self._cache: Dict[str, Dict[str, str]] = {}
        self._cache_size = cache_size
        self._disk_cache: Dict[str, Dict[str, str]] = {}
        self._disk_dirty: bool = False
        self._load_disk_cache()

        # Statistics
        self.cache_hits = 0
        self.disk_cache_hits = 0
        self.api_calls = 0
        self.retries = 0
        self.errors: List[str] = []
        self.total_annotated = 0
        self.total_failed = 0
        self._any_batch_ok = False  # True when at least one API batch succeeds
        self.missed_kanji_sentences: List[str] = []  # kanji sentences that got {} from LLM
        self._had_missed_kanji = False  # True if ANY kanji sentence got no annotations

    # ── Disk cache ────────────────────────────────────────────────────

    def _load_disk_cache(self) -> None:
        """Load persisted readings from disk, sanitizing old entries.

        Old cache may contain corrupted readings (e.g. "=なんだよ" suffixes)
        from previous prompt versions.  These are cleaned on load so they
        don't poison new runs.

        Also runs a lightweight integrity check: if >5% of sampled entries
        have keys that don't appear in their sentences (positional-shift
        corruption), the entire cache is discarded.
        """
        try:
            if _CACHE_FILE.exists():
                raw = _CACHE_FILE.read_text(encoding="utf-8")
                disk_cache: dict = json.loads(raw)
                # Sanitize all cached entries
                cleaned_count = 0
                sanitized: Dict[str, Dict[str, str]] = {}
                for sentence, readings in disk_cache.items():
                    if isinstance(sentence, str) and isinstance(readings, dict):
                        clean = _sanitize_readings_dict({
                            str(k): str(v) for k, v in readings.items()
                        })
                        if clean:
                            sanitized[sentence] = clean
                        if len(clean) != len(readings):
                            cleaned_count += 1

                # ── Integrity check: sample entries for key-sentence mismatch ──
                corrupt = 0
                partial_corrupt = 0  # entries with partial kanji coverage
                sample_size = min(200, len(sanitized))
                sample_items = list(sanitized.items())[:sample_size]
                for sentence, readings in sample_items:
                    if readings and _readings_match_sentence(sentence, readings) == 0.0:
                        corrupt += 1
                    # Also detect partial kanji coverage (LLM missed some words)
                    if readings and _contains_kanji(sentence):
                        kanji_words = _extract_kanji_words(sentence)
                        covered = set(readings.keys())
                        missed = [w for w in kanji_words if not _is_kanji_covered(w, covered)]
                        if missed and len(missed) >= len(kanji_words) * 0.3:
                            partial_corrupt += 1
                if corrupt > sample_size * 0.05:
                    self._log(
                        f"Disk cache appears corrupt "
                        f"({corrupt}/{sample_size} sampled entries have "
                        f"keys not in sentence). Discarding cache.",
                        "error",
                    )
                    self._disk_cache = {}
                    self._disk_dirty = True
                    return
                if partial_corrupt > sample_size * 0.10:
                    self._log(
                        f"Disk cache has too many entries with partial kanji "
                        f"coverage ({partial_corrupt}/{sample_size} sampled). "
                        f"Discarding cache to force clean re-annotation.",
                        "error",
                    )
                    self._disk_cache = {}
                    self._disk_dirty = True
                    return

                self._disk_cache = sanitized
                if cleaned_count > 0:
                    self._disk_dirty = True  # re-save sanitized cache
                    self._log(
                        f"Sanitized {cleaned_count} stale cache entries "
                        f"({len(sanitized)} total sentences loaded)"
                    )
                else:
                    self._log(
                        f"Loaded {len(self._disk_cache)} cached readings from disk"
                    )
        except Exception:
            self._disk_cache = {}

    def _save_disk_cache(self) -> None:
        """Persist cache to disk (thread-safe, debounced)."""
        if not self._disk_dirty:
            return
        with self._lock:
            if not self._disk_dirty:
                return
            try:
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                _CACHE_FILE.write_text(
                    json.dumps(self._disk_cache, ensure_ascii=False),
                    encoding="utf-8",
                )
                self._disk_dirty = False
            except Exception as e:
                self._log(f"Failed to save disk cache: {e}", "warning")

    def _cache_put(self, key: str, value: Dict[str, str]) -> None:
        """Store in both memory and disk cache."""
        # Memory cache (normalized key)
        self._cache[key] = value
        if len(self._cache) > self._cache_size:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        # Disk cache (append-only, save periodically)
        self._disk_cache[key] = value
        self._disk_dirty = True
        # Save every 200 new entries to avoid excessive I/O
        if len(self._disk_cache) % 200 == 0:
            self._save_disk_cache()

    def _cache_get(self, key: str) -> Dict[str, str] | None:
        """Check memory then disk cache."""
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
        if key in self._disk_cache:
            self.disk_cache_hits += 1
            # Promote to memory
            self._cache[key] = self._disk_cache[key]
            return self._disk_cache[key]
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _log(msg: str, level: str = "info") -> None:
        """Timestamped log to stderr AND Python logger."""
        ts = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[LLM {ts}] {msg}"
        print(f"  {full_msg}", file=sys.stderr, flush=True)
        if level == "warning":
            _logger.warning(full_msg)
        elif level == "error":
            _logger.error(full_msg)
        else:
            _logger.info(full_msg)

    # ------------------------------------------------------------------
    # Batch API — the main entry point
    # ------------------------------------------------------------------

    def get_readings_batch(
        self, items: List[str],
    ) -> Dict[str, Dict[str, str]]:
        """Get readings for MANY sentences via batched LLM API calls.

        Args:
            items: List of sentence strings (max context per sentence).
                   The LLM self-tokenizes and annotates purely from context.

        Returns:
            ``{sentence: {substring: reading}}`` — LLM readings.

        Raises:
            LLMBatchError: If all batches fail or return empty.
        """
        if not items:
            return {}

        # Deduplicate AND normalize sentences for better cache hit rate
        seen: Dict[str, str] = {}  # normalized → original
        for s in items:
            norm = _normalize_sentence(s)
            if norm not in seen:
                seen[norm] = s

        result: Dict[str, Dict[str, str]] = {}

        # Check cache (memory + disk)
        uncached: List[str] = []
        for norm, original in seen.items():
            cached = self._cache_get(norm)
            if cached is not None:
                result[original] = cached
            else:
                uncached.append(original)

        if not uncached:
            if self.disk_cache_hits > 0:
                self._log(f"All {len(seen)} sentences served from cache "
                          f"({self.disk_cache_hits} from disk)")
            # ── Kanji coverage check even when all cached ──────────
            self._verify_kanji_coverage(result, seen)
            return result

        # Build chunks
        chunks: List[tuple[int, List[str]]] = []
        bs = self._batch_size
        total_chunks = (len(uncached) + bs - 1) // bs
        for i in range(0, len(uncached), bs):
            chunk = uncached[i : i + bs]
            chunks.append((i // bs + 1, chunk))

        self._log(
            f"{len(uncached)}/{len(seen)} uncached → "
            f"{total_chunks} batch(es) × ~{bs} sentences"
        )

        # Process chunks in parallel
        workers = total_chunks if total_chunks else 1
        if self._max_concurrent > 0:
            workers = min(workers, self._max_concurrent)

        batch_errors: list[str] = []
        completed_chunks = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._process_batch, cn, ch, total_chunks
                ): cn
                for cn, ch in chunks
            }
            for future in as_completed(futures):
                cn = futures[future]
                try:
                    batch_result = future.result()
                except Exception as exc:
                    err_msg = f"Batch {cn}/{total_chunks}: {exc}"
                    self._log(err_msg, "error")
                    batch_errors.append(err_msg)
                    with self._lock:
                        for _cn, _ch in chunks:
                            if _cn == cn:
                                self.total_failed += len(_ch)
                                break
                    completed_chunks += 1
                    if self._progress_callback:
                        self._progress_callback(completed_chunks, total_chunks)
                    continue

                if batch_result is None:
                    err_msg = f"Batch {cn}/{total_chunks} returned None"
                    self._log(err_msg, "error")
                    batch_errors.append(err_msg)
                    completed_chunks += 1
                    if self._progress_callback:
                        self._progress_callback(completed_chunks, total_chunks)
                    continue

                # Cache and merge results (use normalized keys)
                for sentence, readings in batch_result.items():
                    norm = _normalize_sentence(sentence)
                    self._cache_put(norm, readings)
                    result[sentence] = readings
                    with self._lock:
                        self.total_annotated += len(readings)
                        self._any_batch_ok = True

                completed_chunks += 1
                if self._progress_callback:
                    self._progress_callback(completed_chunks, total_chunks)

        # Persist disk cache after all batches complete
        self._save_disk_cache()

        if batch_errors:
            err_detail = "; ".join(batch_errors[-3:])  # last 3 only
            self._log(f"{len(batch_errors)} batch(es) failed", "error")
            # Don't raise if we have SOME results — partial success is OK
            if not result:
                raise LLMBatchError(
                    f"{len(batch_errors)}/{total_chunks} batch(es) failed"
                )

        if not result:
            raise LLMBatchError(
                f"All {len(uncached)} sentences returned empty from LLM"
            )

        # ── Kanji coverage: first pass (non-strict) ────────────────
        zero_missed, partial_missed = self._verify_kanji_coverage(
            result, seen, strict=False
        )

        # ── Repair: retry missed words individually ───────────────
        if zero_missed or partial_missed:
            self._repair_missed_readings(zero_missed, partial_missed, result)
            # Second pass: strict check after repair
            self._verify_kanji_coverage(result, seen, strict=True)
            # Repair succeeded — clear stale flags from first pass
            with self._lock:
                self._had_missed_kanji = False
                self.missed_kanji_sentences.clear()

        # ── Propagate readings to ALL items (including dedup-duplicates) ──
        # The seen-dict dedup in get_readings_batch drops sentences that
        # normalize to the same form.  Those duplicates never got result
        # entries.  After batch processing, the normalized cache contains
        # all the readings — we propagate them back to every item in the
        # original list so _apply_block / _process_text_node find them.
        missing_from_result = 0
        for s in items:
            if s not in result:
                norm = _normalize_sentence(s)
                cached = self._cache_get(norm)
                if cached is not None:
                    result[s] = cached
                    missing_from_result += 1
        if missing_from_result > 0:
            self._log(
                f"Propagated cache readings to {missing_from_result} "
                f"dedup-duplicate item(s) not in original result"
            )

        # ── Final verification: check ALL original items, not just seen ──
        # Build a temporary seen-like dict for the full items list
        full_seen: Dict[str, str] = {}
        for s in items:
            norm = _normalize_sentence(s)
            if norm not in full_seen:
                full_seen[norm] = s
        self._verify_kanji_coverage(result, full_seen, strict=True)

        return result

    def _process_batch(
        self, chunk_num: int,
        chunk: List[str],
        total_chunks: int,
    ) -> Dict[str, Dict[str, str]]:
        """Process one batch: call LLM, return readings. Thread-safe."""
        self._log(
            f"Batch {chunk_num}/{total_chunks} – "
            f"{len(chunk)} sentences – calling API..."
        )

        llm_result = self._retry_with_backoff(chunk)

        # _retry_with_backoff returns {} when it gracefully gave up on
        # all sentences (tiny batch, all retries exhausted).  Raise an
        # error so the caller does NOT cache these empty results — we
        # want to retry them on the next run in case a different model
        # or prompt can handle them.
        if not llm_result:
            self._log(
                f"Batch {chunk_num}/{total_chunks} – "
                f"LLM could not annotate any of the {len(chunk)} sentence(s) "
                f"after all retries",
                "warning",
            )
            raise LLMAPIError(
                f"Batch {chunk_num}/{total_chunks}: all {len(chunk)} "
                f"sentence(s) could not be annotated by LLM"
            )

        words_annotated = sum(len(c) for c in llm_result.values())
        self._log(
            f"Batch {chunk_num}/{total_chunks} – done "
            f"({words_annotated} words in {len(chunk)} sentences)"
        )

        return llm_result

    def _verify_kanji_coverage(
        self,
        result: Dict[str, Dict[str, str]],
        seen: Dict[str, str],
        strict: bool = False,
    ) -> tuple[list[str], list[tuple[str, list[str]]]]:
        """Verify kanji words got annotations.

        Returns ``(zero_missed, partial_missed)`` lists.

        When *strict* is True, raises :exc:`LLMBatchError` if ANY kanji
        sentence has ZERO annotations.  When False, only raises if >50%
        are ZERO (catastrophic failure).
        """
        total = len(seen)
        zero_missed: list[str] = []
        partial_missed: list[tuple[str, list[str]]] = []
        partial_word_count = 0

        kanji_total = 0
        for norm, original in seen.items():
            if not _contains_kanji(norm):
                continue
            kanji_total += 1
            readings = result.get(original, {})
            if not readings:
                zero_missed.append(original)
            else:
                kanji_words = _extract_kanji_words(original)
                covered = set(readings.keys())
                missed_words = [w for w in kanji_words if not _is_kanji_covered(w, covered)]
                if missed_words:
                    partial_missed.append((original, missed_words))
                    partial_word_count += len(missed_words)

        with self._lock:
            for s in self.missed_kanji_sentences:
                # Only count as missed if the sentence STILL has no
                # readings in result (repair may have fixed it).
                if s in result and result[s]:
                    continue  # repaired — not missed anymore
                already_zero = s in zero_missed
                already_partial = any(s == pm[0] for pm in partial_missed)
                if not already_zero and not already_partial:
                    zero_missed.append(s)

        total_missed = len(zero_missed) + len(partial_missed)
        if total_missed > 0:
            parts: list[str] = []
            if zero_missed:
                parts.append(f"{len(zero_missed)} ZERO")
            if partial_missed:
                parts.append(
                    f"{len(partial_missed)} PARTIAL"
                    f" ({partial_word_count} word(s) missed)"
                )
            detail = ", ".join(parts)

            zero_ratio = len(zero_missed) / max(1, kanji_total)
            threshold = 0.0 if strict else 0.5
            if zero_ratio > threshold:
                self._log(
                    f"KANJI COVERAGE ERROR: {len(zero_missed)}/{kanji_total} "
                    f"kanji-containing sentence(s) got ZERO annotations "
                    f"({zero_ratio:.0%}). "
                    f"First 3: {zero_missed[:3]!r}",
                    "error",
                )
                with self._lock:
                    self._had_missed_kanji = True
                raise LLMBatchError(
                    f"{len(zero_missed)}/{kanji_total} kanji-containing sentence(s) "
                    f"got ZERO annotations ({zero_ratio:.0%}). "
                    f"Check API key / model / quota."
                )

            # In strict mode, PARTIAL annotations are also fatal
            if strict and partial_missed:
                self._log(
                    f"KANJI COVERAGE ERROR: {len(partial_missed)}/{kanji_total} "
                    f"kanji-containing sentence(s) have PARTIAL annotations "
                    f"({partial_word_count} word(s) missed). "
                    f"First 3: {[pm[0][:80] for pm in partial_missed[:3]]!r}",
                    "error",
                )
                with self._lock:
                    self._had_missed_kanji = True
                raise LLMBatchError(
                    f"{len(partial_missed)}/{kanji_total} kanji-containing "
                    f"sentence(s) have PARTIAL annotations "
                    f"({partial_word_count} word(s) missed)."
                )

            self._log(
                f"KANJI COVERAGE: {total_missed}/{total} "
                f"sentence(s) with missed annotations ({detail}). "
                f"First 3: {(zero_missed + [pm[0] for pm in partial_missed])[:3]!r}",
                "warning",
            )
            with self._lock:
                self._had_missed_kanji = True

        return zero_missed, partial_missed

    # ------------------------------------------------------------------
    # Per-word repair – retry individual missed kanji words
    # ------------------------------------------------------------------

    _REPAIR_SYSTEM_PROMPT = """\
You are a kanji→hiragana converter. Output ONLY a JSON object.

For the sentence below, give the hiragana reading for the specified kanji words.

CRITICAL:
- Output ONLY: {"word1":"reading1","word2":"reading2",...}
- Each key is the EXACT kanji word from the input
- Each value is PURE HIRAGANA only — no "=", no "()", no spaces
- The sentence provides context for disambiguating readings (e.g. 人→ひと vs にん)

Output ONLY the JSON object. Nothing else."""

    def _repair_missed_readings(
        self,
        zero_missed: list[str],
        partial_missed: list[tuple[str, list[str]]],
        result: dict[str, dict[str, str]],
    ) -> int:
        """Retry individual missed kanji words with sentence context.

        For each missed sentence, sends the FULL sentence as context
        plus a list of specific kanji words to annotate.  This gives
        the LLM enough context to disambiguate readings while keeping
        the prompt small and focused.

        Returns the number of words successfully repaired.
        """
        repair_items: list[tuple[str, list[str]]] = []
        for sentence in zero_missed:
            words = _extract_kanji_words(sentence)
            if words:
                repair_items.append((sentence, words))
        for sentence, missed_words in partial_missed:
            if missed_words:
                repair_items.append((sentence, missed_words))

        if not repair_items:
            return 0

        total_words = sum(len(w) for _, w in repair_items)
        self._log(
            f"Repairing {len(repair_items)} sentence(s) "
            f"({total_words} missed word(s)) with parallel targeted queries..."
        )

        # ── Execute all in parallel ─────────────────────────────
        repaired_count = 0
        # Build word lookup for logging
        words_by_sentence = {s: list(dict.fromkeys(w)) for s, w in repair_items}
        with ThreadPoolExecutor(max_workers=len(repair_items)) as executor:
            futures = {
                executor.submit(
                    self._repair_one_sentence, sentence, words
                ): sentence
                for sentence, words in repair_items
            }
            for future in as_completed(futures):
                sentence, sanitized, error = future.result()
                if sanitized is not None:
                    if sentence in result:
                        result[sentence].update(sanitized)
                    else:
                        result[sentence] = sanitized
                    repaired_count += len(sanitized)
                    unique_words = words_by_sentence.get(sentence, [])
                    self._log(
                        f"  ↻ repaired {len(sanitized)}/{len(unique_words)} "
                        f"word(s): {sentence[:60]}",
                    )
                    still_missed = [w for w in unique_words
                                    if w not in sanitized
                                    and not _is_kanji_covered(w, set(sanitized.keys()))]
                    if still_missed:
                        self._log(
                            f"  ⚠ LLM still missed: {still_missed}",
                            "warning",
                        )
                else:
                    self._log(
                        f"  ✗ repair failed: {sentence[:60]}... ({error})",
                        "warning",
                    )

        self._log(
            f"Repair complete: {repaired_count}/{total_words} word(s) fixed"
        )
        return repaired_count

    def _repair_one_sentence(
        self, sentence: str, words: list[str]
    ) -> tuple[str, dict[str, str] | None, str | None]:
        """Repair a single sentence via LLM. Thread-safe.

        Returns (sentence, readings_dict_or_None, error_msg_or_None).

        Retries ONCE with the same prompt when the LLM returns
        unparseable/malformed content (transient API glitch,
        truncated response, etc.).
        """
        unique_words = list(dict.fromkeys(words))
        word_list = "、".join(unique_words)
        user_prompt = (
            f"Sentence: {sentence}\n"
            f"Annotate these kanji words: {word_list}"
        )

        last_error: str | None = None
        for attempt in range(2):
            try:
                content = self._pool.llm_call(
                    system_prompt=self._REPAIR_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    model=self._model,
                    temperature=0.0,
                    max_tokens=min(4096, self._max_tokens),
                )
                data = _extract_json(content)
                if isinstance(data, dict):
                    sanitized = _sanitize_readings_dict(
                        {str(k): str(v) for k, v in data.items()
                         if isinstance(k, str) and isinstance(v, str)}
                    )
                    if sanitized:
                        return (sentence, sanitized, None)
                    else:
                        last_error = f"sanitized to empty: {content[:100]!r}"
                        if attempt == 0:
                            continue  # retry once with same prompt
                        return (sentence, None, last_error)
                else:
                    last_error = f"parse failed (got {type(data).__name__}): {content[:100]!r}"
                    if attempt == 0:
                        continue  # retry once
                    return (sentence, None, last_error)
            except Exception as exc:
                last_error = str(exc)
                if attempt == 0:
                    continue  # retry once
                return (sentence, None, last_error)

        return (sentence, None, last_error or "unreachable")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _retry_with_backoff(
        self, items: List[str],
    ) -> Dict[str, Dict[str, str]]:
        """Call ``_call_api_batch`` with exponential backoff retry.

        The pool handles provider-level failover internally.
        This method retries on transient errors.

        When the response is **truncated** (finish_reason=length) or the
        batch consistently fails validation (empty objects, garbled JSON),
        the batch is automatically split into smaller halves and retried
        recursively.  This avoids the "stuck batch" problem where the LLM
        repeatedly produces the same invalid output for a given batch size.
        """
        last_error: str | None = None
        recovery_waits = 0  # prevent infinite recovery loops
        for attempt in range(self._max_retries + 1):
            try:
                # Use escalated retry prompt on 2nd+ attempts
                return self._call_api_batch(
                    items, use_retry_prompt=(attempt > 0)
                )
            except APITruncatedError as exc:
                # ── Truncation detected → split batch and recurse ──
                # The LLM hit its output token limit mid-response.
                # Retrying with the same batch would hit the same limit,
                # so we split instead.
                mid = len(items) // 2
                if mid < 1 or len(items) <= 2:
                    msg = (
                        f"Output truncated even for tiny batch "
                        f"({len(items)} sentence(s)). "
                        f"Increase --llm-max-tokens (current: {self._max_tokens})"
                    )
                    self._log(msg, "error")
                    with self._lock:
                        self.errors.append(
                            f"{datetime.now().isoformat()} | {msg}"
                        )
                    raise LLMAPIError(msg) from exc

                self._log(
                    f"Truncation detected → splitting "
                    f"{len(items)} → {mid}+{len(items)-mid}",
                    "warning",
                )
                with self._lock:
                    self.retries += 1
                left = self._retry_with_backoff(items[:mid])
                right = self._retry_with_backoff(items[mid:])
                return {**left, **right}

            except LLMAPIError as exc:
                # ── Batch validation failed → split immediately ──
                # Retrying with the same batch size almost never helps
                # when the LLM produces truncated JSON or empty objects.
                # Splitting is far more effective.
                mid = len(items) // 2
                if mid >= 1 and len(items) > 2:
                    self._log(
                        f"Validation failed → splitting "
                        f"{len(items)} → {mid}+{len(items)-mid} "
                        f"(attempt {attempt + 1}/{self._max_retries + 1})",
                        "warning",
                    )
                    with self._lock:
                        self.retries += 1
                    left = self._retry_with_backoff(items[:mid])
                    right = self._retry_with_backoff(items[mid:])
                    return {**left, **right}

                # ── Tiny batch (≤2 items): retry with escalated prompt ──
                # Don't give up immediately — try at least once more with
                # the retry prompt before accepting defeat.
                if len(items) <= 2 and attempt == 0:
                    self._log(
                        f"Tiny batch ({len(items)} item(s)) failed — "
                        f"retrying with escalated prompt...",
                        "warning",
                    )
                    with self._lock:
                        self.retries += 1
                    continue  # next iteration will use retry prompt

                # ── Tiny batch (≤2 items) still failing after retry ──
                # After repeated splits + retries we've reached the minimum
                # size and the LLM still can't annotate these sentences.
                # Accept defeat gracefully: return empty so the caller
                # can move on, rather than aborting the entire EPUB.
                if len(items) <= 2:
                    self._log(
                        f"Giving up on {len(items)} sentence(s) after "
                        f"{attempt + 1} attempt(s) — LLM cannot annotate",
                        "warning",
                    )
                    with self._lock:
                        self.total_failed += len(items)
                        self.retries += 1
                    return {}  # empty = no readings for these items

                # Batch too small to split — normal retry
                last_error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    self.retries += 1
                if attempt < self._max_retries:
                    wait = 2 ** attempt
                    self._log(
                        f"Retry {attempt + 1}/{self._max_retries} in {wait}s "
                        f"({last_error})",
                        "warning"
                    )
                    time.sleep(wait)
                else:
                    # Retries exhausted on a small batch → give up gracefully
                    self._log(
                        f"Giving up on {len(items)} sentence(s) after "
                        f"{self._max_retries} retries — LLM cannot annotate",
                        "warning",
                    )
                    with self._lock:
                        self.total_failed += len(items)
                    return {}

            except APIPoolExhaustedError as exc:
                # All providers are paused (quota/rate-limit exhausted)
                # OR the pool is dead from a permanent error.
                # Retrying with short backoff is pointless — we need to
                # wait for a provider's pause to expire, or fail fast.

                n = self._pool.provider_count
                is_dead = getattr(self._pool, "dead", False)
                dead_reason = getattr(self._pool, "dead_reason", "")

                last_error = f"APIPoolExhaustedError: {exc}"

                if is_dead:
                    # Permanent error (404, 401, etc.) – retrying won't help
                    msg = f"API FAILED – {dead_reason or exc}"
                    self._log(msg, "error")
                    with self._lock:
                        self.errors.append(
                            f"{datetime.now().isoformat()} | {last_error}"
                        )
                    raise LLMAPIError(msg) from exc

                earliest = exc.earliest_resume
                remaining = max(0.0, earliest - time.time()) if earliest > 0 else float("inf")

                if n == 1:
                    # Single provider: retrying won't help, fail immediately
                    msg = (
                        f"API FAILED – only provider exhausted "
                        f"(quota resets in ~{remaining:.0f}s)"
                    )
                    self._log(msg, "error")
                    with self._lock:
                        self.errors.append(
                            f"{datetime.now().isoformat()} | {last_error}"
                        )
                    raise LLMAPIError(msg) from exc

                # Multiple providers: wait for earliest to become available
                if earliest > 0 and remaining < 300 and recovery_waits < self._max_retries:
                    recovery_waits += 1
                    self._log(
                        f"All {n} providers exhausted – "
                        f"waiting {remaining:.0f}s for earliest to recover "
                        f"(recovery wait {recovery_waits}/{self._max_retries})..."
                    )
                    time.sleep(remaining + 1)  # +1s buffer
                    continue  # retry same attempt after waiting

                # Earliest resume too far or unknown → fail fast
                msg = f"API FAILED – all {n} providers exhausted"
                self._log(msg, "error")
                with self._lock:
                    self.errors.append(
                        f"{datetime.now().isoformat()} | {last_error}"
                    )
                raise LLMAPIError(msg) from exc

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    self.retries += 1
                if attempt < self._max_retries:
                    wait = 2 ** attempt
                    self._log(
                        f"Retry {attempt + 1}/{self._max_retries} in {wait}s "
                        f"({last_error})",
                        "warning"
                    )
                    time.sleep(wait)
                else:
                    msg = f"API FAILED after {self._max_retries} retries"
                    self._log(msg, "error")
                    with self._lock:
                        self.errors.append(
                            f"{datetime.now().isoformat()} | "
                            f"{last_error}\n{traceback.format_exc()}"
                        )
                    raise LLMAPIError(msg) from exc

        raise LLMAPIError("Retry loop exhausted unexpectedly")

    # ── Batch API call (JSON structured output) ────────────────────────

    # Response format for OpenAI-compatible APIs — forces JSON output
    # Per DeepSeek official docs: {"type": "json_object"}
    _RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}

    # DeepSeek v4 context window (per official docs)
    _DEEPSEEK_CONTEXT_LIMIT: int = 1_000_000   # 1M tokens

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Conservative token estimation.

        Japanese text: ~1.2–1.5 chars/token in most tokenizers.
        We use 2 chars/token as a safe upper bound to never underestimate.
        """
        if not text:
            return 0
        # Conservative: 2 characters per token
        return max(1, len(text) // 2) + 1

    def _call_api_batch(
        self, items: List[str],
        use_retry_prompt: bool = False,
    ) -> Dict[str, Dict[str, str]]:
        """One LLM API call for MANY sentences.  Thread-safe.

        Uses **JSON structured output** with ``response_format``
        enforcement per DeepSeek official docs.

        Format:
            Input:  ``[N] sentence text`` (numbered lines, N starting at 1)
            Output: ``{"r": [{...}, {...}, ...]}`` (1:1 positional)

        When the LLM returns invalid JSON or too few readings, raises
        ``LLMAPIError`` so ``_retry_with_backoff`` can retry with the
        same json_mode prompt (up to ``max_retries`` times).

        **Overflow protection**: if the estimated total tokens
        (prompt + max_tokens) exceed the 1M context window, the batch
        is automatically split into smaller recursive calls.

        Args:
            use_retry_prompt: If True, use the escalated retry prompt
                (``_SYSTEM_PROMPT_BATCH_RETRY``) which more forcefully
                instructs the LLM to output valid JSON.
        """
        if not items:
            return {}

        # Choose prompt based on whether this is a retry
        system_prompt = (
            self._SYSTEM_PROMPT_BATCH_RETRY if use_retry_prompt
            else self._SYSTEM_PROMPT_BATCH
        )

        # ── Overflow check: split batch if estimated total exceeds context ──
        input_lines = [f"[{i + 1}] {s}" for i, s in enumerate(items)]
        user_prompt = "\n".join(input_lines)
        prompt_estimate = self._estimate_tokens(
            system_prompt + user_prompt
        )

        if prompt_estimate + self._max_tokens > self._DEEPSEEK_CONTEXT_LIMIT:
            # Batch too large → split and recurse
            mid = len(items) // 2
            if mid == 0:
                raise LLMAPIError(
                    f"Single item too large for context window "
                    f"(est. {prompt_estimate} prompt tokens "
                    f"+ {self._max_tokens} max output > "
                    f"{self._DEEPSEEK_CONTEXT_LIMIT} context)"
                )
            self._log(
                f"Batch overflow detected (est. {prompt_estimate}"
                f"+{self._max_tokens}"
                f" > {self._DEEPSEEK_CONTEXT_LIMIT}), "
                f"splitting {len(items)} → {mid}+{len(items)-mid}"
            )
            left = self._call_api_batch(items[:mid], use_retry_prompt=use_retry_prompt)
            right = self._call_api_batch(items[mid:], use_retry_prompt=use_retry_prompt)
            return {**left, **right}

        with self._lock:
            self._rate_limit()
            self.api_calls += 1

        # Single json_mode call — retries are handled by _retry_with_backoff
        try:
            content = self._call_with_concurrency_limit(
                self._pool.llm_call,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=self._model,
                temperature=0.0,
                response_format=self._RESPONSE_FORMAT,
                max_tokens=self._max_tokens,
            )
        except APITruncatedError:
            # Propagate without wrapping — _retry_with_backoff will split
            raise
        except Exception as exc:
            raise LLMAPIError(
                f"LLM API call failed: {exc}"
            ) from exc

        # ── Parse & extract ALL readings directly ─────────────────
        # We bypass _validate_batch_response for the primary path
        # because the LLM may output more items than sentences
        # (e.g. alternating [{readings},{},{readings},{}...]).
        # _validate_batch_response truncates to item_count, losing
        # readings from items beyond that limit.
        # Instead we directly sanitize ALL raw items into a flat dict.
        data = _extract_json(content)

        # Compute which items have kanji (for missed-annotation detection)
        item_has_kanji = [_contains_kanji(s) for s in items]

        raw_list = data.get("r") or data.get("results") or []
        all_readings: dict[str, str] = {}
        if isinstance(raw_list, list):
            for item in raw_list:
                if isinstance(item, dict):
                    # Build raw dict, converting all values to str
                    raw_dict: dict[str, str] = {}
                    for k, v in item.items():
                        if isinstance(k, str) and k.strip():
                            raw_dict[k.strip()] = str(v) if isinstance(v, str) else str(v)
                    sanitized = _sanitize_readings_dict(raw_dict)
                    for word, reading in sanitized.items():
                        if word not in all_readings:
                            all_readings[word] = reading

        if all_readings:
            # ── Match readings to sentences ──────────────────────
            result: Dict[str, Dict[str, str]] = {}
            partial_missed_count = 0
            partial_missed_words_total = 0
            for i, sentence in enumerate(items):
                sent_norm = unicodedata.normalize("NFKC", sentence)
                sent_readings: dict[str, str] = {}
                for word, reading in all_readings.items():
                    # Use EXACT NFKC substring matching — must be
                    # consistent with _segment_text_with_readings()
                    # which uses text_norm.startswith(key, i).
                    # Permissive kanji-sequence matching (the
                    # fallback in _kanji_sequence_in_text) would
                    # assign readings that cannot be applied later.
                    word_norm = unicodedata.normalize("NFKC", word)
                    if word_norm in sent_norm:
                        sent_readings[word] = reading
                if sent_readings:
                    result[sentence] = sent_readings
                    # ── Partial-miss detection ──────────────────
                    if item_has_kanji[i]:
                        kanji_words = _extract_kanji_words(sentence)
                        covered = set(sent_readings.keys())
                        missed_words = [w for w in kanji_words if not _is_kanji_covered(w, covered)]
                        if missed_words:
                            partial_missed_count += 1
                            partial_missed_words_total += len(missed_words)
                            with self._lock:
                                if sentence not in self.missed_kanji_sentences:
                                    self.missed_kanji_sentences.append(sentence)
                                self._had_missed_kanji = True
                elif item_has_kanji[i]:
                    with self._lock:
                        if sentence not in self.missed_kanji_sentences:
                            self.missed_kanji_sentences.append(sentence)
                        self._had_missed_kanji = True

            if result:
                # Log statistics
                non_empty_count = len(result)
                kanji_count = sum(1 for b in item_has_kanji if b)
                # Compute batch_missed from actual ZERO sentences
                batch_missed = sum(
                    1 for i, b in enumerate(item_has_kanji)
                    if b and items[i] not in result
                )
                # ── Reject if too few kanji sentences covered ─────
                # Even if some sentences got readings, the batch may
                # be too sparse to be useful.  Rejecting here causes
                # _retry_with_backoff to split the batch into smaller
                # pieces where the LLM can be more thorough.
                if kanji_count > 0 and non_empty_count < kanji_count * 0.3:
                    self._log(
                        f"Batch result too sparse "
                        f"({non_empty_count}/{kanji_count} kanji sentences, "
                        f"< 30%) — rejecting so batch can be split",
                        "warning",
                    )
                    # Fall through to salvage/error path
                else:
                    if batch_missed > 0:
                        self._log(
                            f"Batch has {batch_missed} kanji sentence(s) "
                            f"with ZERO annotations (LLM missed them)",
                            "error",
                        )
                    if partial_missed_count > 0:
                        self._log(
                            f"Batch has {partial_missed_count}/{kanji_count} "
                            f"kanji items with PARTIAL annotations "
                            f"({partial_missed_words_total} word(s) missed)",
                            "error",
                        )
                    return result

            # result is empty — no sentences got readings at all
            else:
                self._log(
                    f"Flattened dict had {len(all_readings)} readings but "
                    f"none matched any sentence — treating as invalid",
                    "warning",
                )

        # ── Primary validation failed — try to salvage partial results ──
        # When the LLM output is truncated mid-array, many items may be
        # complete and parseable.  We walk the raw JSON to extract those
        # so they aren't wasted.
        salvaged_items, salvaged_count = _salvage_partial_json(
            content, len(items)
        )
        if salvaged_items is not None and salvaged_count > 0:
            # Pad to full length, then validate
            padded = list(salvaged_items)
            while len(padded) < len(items):
                padded.append({})
            padded = padded[:len(items)]
            missed_kanji_indices2: list[int] = []

            validated2 = self._validate_batch_response(
                {"r": padded}, len(items),
                item_has_kanji=item_has_kanji,
                missed_kanji_indices=missed_kanji_indices2,
                skip_quality_gates=True,
            )
            if validated2 is not None:
                # ── Same flat-dict + substring matching as primary ──
                all_readings2: dict[str, str] = {}
                for readings_dict in validated2:
                    for word, reading in readings_dict.items():
                        if word not in all_readings2:
                            all_readings2[word] = reading

                result2: Dict[str, Dict[str, str]] = {}
                partial_missed2 = 0
                partial_missed_words2 = 0
                for i, sentence in enumerate(items):
                    sent_norm = unicodedata.normalize("NFKC", sentence)
                    sent_readings: dict[str, str] = {}
                    for word, reading in all_readings2.items():
                        # EXACT NFKC substring match (consistent with
                        # _segment_text_with_readings)
                        word_norm = unicodedata.normalize("NFKC", word)
                        if word_norm in sent_norm:
                            sent_readings[word] = reading
                    if sent_readings:
                        result2[sentence] = sent_readings
                        if item_has_kanji[i]:
                            kanji_words = _extract_kanji_words(sentence)
                            covered = set(sent_readings.keys())
                            missed_words = [w for w in kanji_words if not _is_kanji_covered(w, covered)]
                            if missed_words:
                                partial_missed2 += 1
                                partial_missed_words2 += len(missed_words)
                                with self._lock:
                                    if sentence not in self.missed_kanji_sentences:
                                        self.missed_kanji_sentences.append(sentence)
                                    self._had_missed_kanji = True
                    elif item_has_kanji[i]:
                        with self._lock:
                            if sentence not in self.missed_kanji_sentences:
                                self.missed_kanji_sentences.append(sentence)
                            self._had_missed_kanji = True

                if result2:
                    non_empty2 = len(result2)
                    batch_missed2 = sum(
                        1 for idx in missed_kanji_indices2
                        if idx < len(item_has_kanji) and item_has_kanji[idx]
                    )
                    kanji_count = sum(1 for b in item_has_kanji if b)
                    # ── Reject salvage if too few kanji sentences covered ──
                    # A salvage of 1/60 items covering only a few sentences
                    # is worse than splitting and retrying with smaller batches.
                    if kanji_count > 0 and non_empty2 < kanji_count * 0.5:
                        self._log(
                            f"Salvaged result too sparse "
                            f"({non_empty2}/{kanji_count} kanji sentences, "
                            f"< 50%) — rejecting so batch can be split",
                            "warning",
                        )
                        # Fall through to error path → split/retry
                    else:
                        if batch_missed2 > 0:
                            self._log(
                                f"Salvaged response has {batch_missed2} kanji "
                                f"sentence(s) with ZERO annotations",
                                "error",
                            )
                        if partial_missed2 > 0:
                            self._log(
                                f"Salvaged response has {partial_missed2} kanji "
                                f"sentence(s) with PARTIAL annotations "
                                f"({partial_missed_words2} word(s) missed)",
                                "error",
                            )
                        self._log(
                            f"Salvaged {salvaged_count}/{len(items)} items "
                            f"from truncated response "
                            f"({non_empty2} valid after sanitization)",
                            "warning",
                        )
                        return result2

        # ── Nothing salvageable — raise error for retry/split ──
        raw_preview = content[:300]
        if _looks_truncated(content):
            self._log(
                f"LLM response appears TRUNCATED. Raw: {raw_preview!r}",
                "warning",
            )
        else:
            self._log(
                f"LLM returned invalid response. Raw: {raw_preview!r}",
                "warning",
            )
        raise LLMAPIError(
            f"LLM returned invalid response "
            f"(first 200 chars: {content[:200]!r})"
        )

    def _call_with_concurrency_limit(self, fn, **kwargs: Any) -> str:
        """Execute *fn(**kwargs)* respecting ``max_concurrent`` limit."""
        if self._concurrency_sem is not None:
            self._concurrency_sem.acquire()
            try:
                return fn(**kwargs)  # type: ignore[no-any-return]
            finally:
                self._concurrency_sem.release()
        else:
            return fn(**kwargs)  # type: ignore[no-any-return]

    def _rate_limit(self) -> None:
        """Ensure minimum delay between API calls."""
        now = time.time()
        elapsed = now - self._last_call_time
        if elapsed < self._rate_limit_delay:
            time.sleep(self._rate_limit_delay - elapsed)
        self._last_call_time = time.time()

    @property
    def stats(self) -> Dict[str, int]:
        """Return cache/API statistics."""
        return {
            "cache_size": len(self._cache),
            "disk_cache_size": len(self._disk_cache),
            "cache_hits": self.cache_hits,
            "disk_cache_hits": self.disk_cache_hits,
            "api_calls": self.api_calls,
            "retries": self.retries,
            "errors": len(self.errors),
            "total_annotated": self.total_annotated,
            "total_failed": self.total_failed,
            "any_batch_ok": int(self._any_batch_ok),
            "missed_kanji": len(self.missed_kanji_sentences),
        }

    @property
    def any_batch_succeeded(self) -> bool:
        """``True`` if at least one API batch succeeded this run."""
        return self._any_batch_ok

    @property
    def had_missed_kanji(self) -> bool:
        """``True`` if any kanji-containing sentence got ZERO annotations from LLM."""
        return self._had_missed_kanji
