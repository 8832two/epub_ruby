from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from .api_pool import APIPool, APIConfig, APIPoolExhaustedError
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
                try:
                    return json.loads(text[start : i + 1])  # type: ignore[no-any-return]
                except json.JSONDecodeError:
                    return {}
    return {}


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

For each numbered input line, find ALL kanji words and return their hiragana reading.

CRITICAL FORMAT RULES:
- Output ONLY: {"r":[{...},{...},...]}
- One {...} object per input line, in the SAME order
- Each key is a kanji word EXACTLY as it appears in the input — do NOT use "kanji" or "word" as the key!
- Each value is PURE HIRAGANA only — no "=", no "()", no spaces, no notes
- Use {} for lines with no kanji

READING VALUE FORMAT:
  "kanji":"hiragana_only"
  ✓ "勉強":"べんきょう", "走った":"はしった", "周囲":"しゅうい"
  ✗ "勉強":"べんきょう=study", "走":"はしった (ran)", "周囲":"しゅういに=に"
  ✗ {"kanji":"はしった"} — NEVER use "kanji" as the key! Use the actual word!

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

Format: {"r":[{"kanji":"hiragana",...},...]}

CRITICAL: Each key MUST be the actual kanji word from the input, NOT "kanji" or "word"!
Each value must be PURE HIRAGANA — no "=", no "()", no extra text.
Use {} for lines with no kanji.

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
                        _PLACEHOLDER_KEYS = {"kanji", "word", "reading", "term", "text", "hiragana"}
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
    ) -> list[dict[str, str]] | None:
        """Validate AND sanitize the parsed JSON response structure.

        Returns a list of ``{word: reading}`` dicts (length = *item_count*)
        with only valid, clean readings — or ``None`` if the structure is
        fundamentally invalid.

        Each per-line dict is run through :func:`_sanitize_readings_dict`
        to strip:
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

        # Pad or truncate to match item_count
        result: list[dict[str, str]] = []
        empty_key_count = 0   # items where ALL keys are empty strings
        useless_item_count = 0  # items that had raw entries but all were filtered out
        placeholder_key_count = 0  # items using literal "kanji"/"word" as key (LLM confused)
        _PLACEHOLDER_KEYS = {"kanji", "word", "reading", "term", "text", "hiragana"}
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

        # ── Quality gate: reject responses that are structurally valid
        #     but semantically empty (LLM outputting empty keys, regurgitated
        #     sentences, etc.) ──────────────────────────────────────────
        total_raw_items = min(len(raw), item_count)
        if total_raw_items > 0:
            # If > 30% of items have empty keys OR > 50% of items are
            # useless (had content but all filtered), treat as invalid
            if empty_key_count > total_raw_items * 0.3:
                return None
            if useless_item_count > total_raw_items * 0.5:
                return None
            # If > 30% use placeholder keys ("kanji" instead of actual word)
            if placeholder_key_count > total_raw_items * 0.3:
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
        max_retries: int = 3,
        max_concurrent: int = 0,
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

    # ── Disk cache ────────────────────────────────────────────────────

    def _load_disk_cache(self) -> None:
        """Load persisted readings from disk, sanitizing old entries.

        Old cache may contain corrupted readings (e.g. "=なんだよ" suffixes)
        from previous prompt versions.  These are cleaned on load so they
        don't poison new runs.
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

        if not llm_result:
            msg = f"Batch {chunk_num}/{total_chunks} – LLM returned empty"
            self._log(msg, "error")
            raise LLMAPIError(msg)

        words_annotated = sum(len(c) for c in llm_result.values())
        self._log(
            f"Batch {chunk_num}/{total_chunks} – done "
            f"({words_annotated} words in {len(chunk)} sentences)"
        )

        return llm_result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _retry_with_backoff(
        self, items: List[str],
    ) -> Dict[str, Dict[str, str]]:
        """Call ``_call_api_batch`` with exponential backoff retry.

        The pool handles provider-level failover internally.
        This method retries on transient errors.

        When all providers are exhausted (quota/rate-limit), the retry
        strategy adapts: single-provider pools fail immediately (retrying
        won't help), while multi-provider pools wait for the earliest
        provider to become available.
        """
        last_error: str | None = None
        recovery_waits = 0  # prevent infinite recovery loops
        for attempt in range(self._max_retries + 1):
            try:
                return self._call_api_batch(items)
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
                # ── Non-transient failures: off-task / invalid format ──
                # _call_api_batch already tried both primary & escalated
                # prompts internally.  If it still failed, retrying with
                # backoff won't help — the LLM fundamentally won't comply.
                err_msg = str(exc).lower()
                if ("off-task" in err_msg or
                        "non-json" in err_msg or
                        "invalid response" in err_msg):
                    self._log(
                        f"LLM off-task error (not retryable): {exc}",
                        "error",
                    )
                    raise

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
    _RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}

    def _call_api_batch(
        self, items: List[str],
    ) -> Dict[str, Dict[str, str]]:
        """One LLM API call for MANY sentences.  Thread-safe.

        Uses **JSON structured output** with ``response_format``
        enforcement.  The compact ``word=reading`` positional format has
        been replaced because LLMs would ignore it and output translations
        or literary analysis instead.

        Format:
            Input:  ``[N] sentence text`` (numbered lines, N starting at 1)
            Output: ``{"r": [{...}, {...}, ...]}`` (1:1 positional)

        When the LLM goes off-task (translation, analysis, etc.), the
        method retries with an escalated system prompt that explicitly
        calls out the previous failure.
        """
        with self._lock:
            self._rate_limit()
            self.api_calls += 1

        if not items:
            return {}

        # Build numbered input lines for clearer positional context
        input_lines = [f"[{i + 1}] {s}" for i, s in enumerate(items)]
        user_prompt = "\n".join(input_lines)

        # Try primary prompt first, then escalate to retry prompt,
        # then try without response_format enforcement (some models
        # misbehave with json_object mode).
        prompts = [
            (self._SYSTEM_PROMPT_BATCH, False, True),       # primary + json_mode
            (self._SYSTEM_PROMPT_BATCH_RETRY, True, True),  # escalated + json_mode
            (self._SYSTEM_PROMPT_BATCH, False, False),      # primary, NO json_mode
        ]

        last_raw: str = ""
        last_error: str = ""

        for prompt_idx, (system_prompt, is_retry, use_json_mode) in enumerate(prompts):
            try:
                content = self._call_with_concurrency_limit(
                    self._pool.llm_call,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    model=self._model,
                    temperature=0.0,           # deterministic output
                    response_format=self._RESPONSE_FORMAT if use_json_mode else None,
                )
            except Exception as exc:
                # API-level failure — don't retry with different prompt
                raise LLMAPIError(
                    f"LLM API call failed: {exc}"
                ) from exc

            last_raw = content

            # ── Parse & validate ──────────────────────────────────
            data = _extract_json(content)

            # Check if the JSON data looks off-task
            if isinstance(data, dict):
                # Detect off-task keys like "analysis", "translation", etc.
                off_task_keys = {
                    "analysis", "translation", "summary", "commentary",
                    "explanation", "continuation", "characters",
                    "剧情", "分析", "翻译", "评论",
                }
                if off_task_keys & set(data.keys()):
                    last_error = (
                        f"LLM returned off-task JSON keys: "
                        f"{sorted(off_task_keys & set(data.keys()))}"
                    )
                    if not is_last_attempt:
                        self._log(
                            f"LLM returned off-task data, retrying "
                            f"with escalated prompt... ({last_error})",
                            "warning",
                        )
                        continue  # try next prompt
                    else:
                        raw_preview = content[:300]
                        self._log(
                            f"LLM returned off-task data after all "
                            f"attempts. Raw: {raw_preview!r}",
                            "error",
                        )
                        raise LLMAPIError(
                            f"LLM returned off-task response: {last_error}"
                        )

            validated = self._validate_batch_response(data, len(items))

            if validated is not None:
                # Success — build result dict
                result: Dict[str, Dict[str, str]] = {}
                non_empty_count = 0
                for i, sentence in enumerate(items):
                    readings = validated[i]
                    if readings:
                        non_empty_count += 1
                        result[sentence] = readings
                if result:
                    # Quality gate: at least 10% of items should have readings
                    if non_empty_count >= len(items) * 0.1 or len(items) <= 5:
                        return result
                    # Too few useful readings — treat as failed validation
                    self._log(
                        f"Only {non_empty_count}/{len(items)} items got readings "
                        f"(< 10%), treating as invalid",
                        "warning",
                    )

            # ── Validation failed ─────────────────────────────────
            is_last_attempt = (prompt_idx == len(prompts) - 1)

            if not is_retry and not is_last_attempt:
                # Check if the raw text looks off-task
                if self._is_off_task_response(content):
                    last_error = "LLM returned translation/analysis instead of JSON"
                    self._log(
                        f"LLM went off-task ({last_error}), "
                        f"retrying with escalated prompt...",
                        "warning",
                    )
                    continue  # try retry prompt

                # Not clearly off-task but still invalid JSON
                last_error = f"LLM returned invalid/non-JSON response"
                self._log(
                    f"{last_error}, retrying with escalated prompt...",
                    "warning",
                )
                continue  # try retry prompt

            # ── Failed on retry or last-resort attempt ────────────
            raw_preview = content[:300]
            if is_retry:
                # Escalated prompt also failed — try without json_mode
                self._log(
                    f"LLM returned invalid response even after retry. "
                    f"Trying without JSON mode...",
                    "warning",
                )
                continue  # try third attempt (no json_mode)

            # All attempts exhausted
            self._log(
                f"LLM returned invalid response after all attempts. "
                f"Raw: {raw_preview!r}",
                "error",
            )
            raise LLMAPIError(
                f"LLM returned invalid response after all attempts "
                f"(first 200 chars: {content[:200]!r})"
            )

        # Should not reach here
        raise LLMAPIError(
            f"LLM returned empty/invalid response after all retries"
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
        }

    @property
    def any_batch_succeeded(self) -> bool:
        """``True`` if at least one API batch succeeded this run."""
        return self._any_batch_ok
