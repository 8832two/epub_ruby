"""
LLM-based kanji reading using multiple API providers for context-aware furigana.

Key insight: a kanji character can have multiple readings (onyomi/kunyomi),
and the correct reading can only be determined by putting the word back into
the original sentence.  This module uses LLM APIs to do exactly that.

Architecture:
  - fugashi (UniDic) handles tokenization → reliable word boundaries
  - LLM (DeepSeek / OpenAI / Gemini) annotates ALL kanji words from
    context alone (NO dictionary hints that could mislead the LLM)
  - Katakana loanwords still use fugashi's lemma (English gloss)
  - NO fugashi fallback — failed batches are skipped, errors are logged
  - Built-in LRU cache + API pool with automatic provider failover
  - Configurable batch size for API calls

Supported providers:
  - DeepSeek (deepseek)     – OpenAI-compatible, paid
  - OpenAI (openai)          – OpenAI-compatible, paid
  - Gemini (gemini)          – google-genai SDK, free tier available
"""

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
from typing import Any, Dict, List, Optional, Callable

from .api_pool import APIPool, APIConfig, APIPoolExhaustedError

_logger = logging.getLogger("epub_ruby")

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

    Usage::

        # Single provider (backward-compatible)
        reader = LLMRubyReader(api_key="sk-...", model="deepseek-v4-flash")

        # Multiple providers with failover
        pool = APIPool([
            APIConfig(provider="deepseek", api_key="sk-...", model="deepseek-v4-flash"),
            APIConfig(provider="gemini", api_key="...", model="gemini-3-flash-preview"),
        ])
        reader = LLMRubyReader(pool=pool)

        readings = reader.get_readings("私は毎日日本語を勉強します",
                                       ["私", "毎日", "日本語", "勉強"])
        # → {"私": "わたし", "毎日": "まいにち",
        #     "日本語": "にほんご", "勉強": "べんきょう"}
    """

    def __init__(
        self,
        # -- New API: pass an APIPool directly --
        pool: Optional[APIPool] = None,
        # -- Legacy API (backward-compatible) --
        api_key: Optional[str] = None,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        # -- Common settings --
        batch_size: int = 60,
        cache_size: int = 8192,
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
                Defaults to ``DEEPSEEK_API_KEY`` env var.
            model: Model name (legacy). Use ``deepseek-v4-flash`` for speed
                or ``deepseek-v4-pro`` for quality.
            base_url: API base URL (legacy).
            batch_size: Max sentences per API call (default 60).
                Smaller = faster per-call, larger = fewer calls.
            cache_size: Max number of cached sentence→readings mappings.
            rate_limit_delay: Seconds to wait between API calls (default 0.1s).
            timeout: HTTP request timeout in seconds (default 120s).
            max_retries: Max retries per failed API call (default 3).
            max_concurrent: Max simultaneous API calls (default 0 = unlimited).
                Set to e.g. 5 for Gemini free tier (15 RPM).  This limits both
                intra-file batch parallelism AND cross-file parallelism.
            progress_callback: Optional fn(completed_chunks, total_chunks)
                called after each batch completes.
        """
        if pool is not None:
            self._pool = pool
        else:
            # Legacy single-provider mode
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

        self._model = model  # fallback model name
        self._batch_size = batch_size
        self._cache: Dict[str, Dict[str, str]] = {}
        self._cache_size = cache_size
        self._rate_limit_delay = rate_limit_delay
        self._last_call_time = 0.0
        self._max_retries = max_retries
        self._lock = threading.Lock()
        self._progress_callback = progress_callback

        # Concurrency control: limit simultaneous API calls.
        # 0 = unlimited; set to e.g. 5 for Gemini free tier (15 RPM).
        self._max_concurrent = max_concurrent
        self._concurrency_sem: threading.Semaphore | None = (
            threading.Semaphore(max_concurrent) if max_concurrent > 0 else None
        )

        # Statistics
        self.cache_hits = 0
        self.api_calls = 0
        self.retries = 0
        self.errors: List[str] = []
        self.total_annotated = 0
        self.total_failed = 0

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

    def get_readings(self, sentence: str, words: List[str]) -> Dict[str, str]:
        """Get context-aware substring readings for a sentence.

        The LLM self-tokenizes the sentence and returns the exact substrings
        that should receive ruby annotation.
        """
        cache_key = sentence
        if cache_key in self._cache:
            self.cache_hits += 1
            return self._cache[cache_key]

        # Build prompt & call API
        try:
            result = self._call_api(sentence)
        except Exception as exc:
            print(f"  [LLM] API error: {exc}")
            return {}

        # Cache
        self._evict_if_needed()
        self._cache[cache_key] = result

        return result

    # ------------------------------------------------------------------
    # Batch API (one call per file, NOT per sentence)
    # ------------------------------------------------------------------

    def get_readings_batch(
        self, items: List[tuple[str, List[str]]],
    ) -> Dict[str, Dict[str, str]]:
        """Get readings for MANY sentences via batched LLM API calls.

        Args:
            items: List of ``(sentence, [word, ...])`` tuples.
                   Each tuple is a sentence and the words in it that
                   need furigana annotation.  NO dictionary hints are
                   sent — the LLM annotates purely from context.

        Returns:
            ``{sentence: {substring: reading}}`` — LLM readings.
            Sentences from failed batches are simply absent from the
            result (they will appear without ruby in the output).
        """
        if not items:
            return {}

        # Deduplicate sentences
        seen: Dict[str, None] = {}
        for sentence, _words in items:
            seen[sentence] = None

        result: Dict[str, Dict[str, str]] = {}

        # Check cache first
        uncached: List[tuple[str, List[str]]] = []
        for sentence in seen:
            if sentence in self._cache:
                self.cache_hits += 1
                result[sentence] = self._cache[sentence]
            else:
                uncached.append((sentence, []))

        if not uncached:
            return result

        # Build chunks based on configurable batch size
        chunks: List[tuple[int, List[tuple[str, List[str]]]]] = []
        bs = self._batch_size
        total_chunks = (len(uncached) + bs - 1) // bs
        for i in range(0, len(uncached), bs):
            chunk = uncached[i : i + bs]
            chunk_num = i // bs + 1
            chunks.append((chunk_num, chunk))

        self._log(
            f"Dispatching {total_chunks} batch(es) × ~{bs} "
            f"sentences – all in parallel"
        )

        # Process ALL chunks in parallel – the max_concurrent semaphore
        # (if configured) is the only gate on actual API call concurrency.
        workers = total_chunks if total_chunks else 1
        if self._max_concurrent > 0:
            workers = min(workers, self._max_concurrent)

        failed_sentences = 0
        completed_chunks = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._process_batch, chunk_num, chunk, total_chunks): chunk_num
                for chunk_num, chunk in chunks
            }
            for future in as_completed(futures):
                chunk_num = futures[future]
                try:
                    batch_result = future.result()
                except Exception as exc:
                    self._log(f"Batch {chunk_num} – unexpected error: {exc}", "error")
                    batch_result = None

                if batch_result is None:
                    # Batch failed → skip these sentences (no ruby for them)
                    for cn, ch in chunks:
                        if cn == chunk_num:
                            failed_sentences += len(ch)
                            with self._lock:
                                self.total_failed += len(ch)
                                self.errors.append(
                                    f"{datetime.now().isoformat()} | "
                                    f"Batch {chunk_num}/{total_chunks} FAILED "
                                    f"({len(ch)} sentences) – no ruby for these"
                                )
                            break
                    # Still count failed batch for progress
                    completed_chunks += 1
                    if self._progress_callback:
                        self._progress_callback(completed_chunks, total_chunks)
                    continue

                # Store LLM readings directly (no fugashi merge)
                for sentence, readings in batch_result.items():
                    self._evict_if_needed()
                    self._cache[sentence] = readings
                    result[sentence] = readings
                    with self._lock:
                        self.total_annotated += len(readings)

                # Update progress after each successful batch
                completed_chunks += 1
                if self._progress_callback:
                    self._progress_callback(completed_chunks, total_chunks)

        if failed_sentences:
            self._log(
                f"⚠ {failed_sentences} sentence(s) in failed batches "
                f"– no ruby annotation for these", "warning"
            )

        return result

    def _process_batch(
        self, chunk_num: int,
        chunk: List[tuple[str, List[str]]],
        total_chunks: int,
    ) -> Dict[str, Dict[str, str]] | None:
        """Process one batch: call LLM, return readings directly.  Thread-safe.

        No fugashi fallback — if the LLM fails, this batch returns None
        and those sentences get no ruby annotation.
        """
        self._log(
            f"Batch {chunk_num}/{total_chunks} – "
            f"{len(chunk)} sentences – calling API..."
        )

        llm_result = self._retry_with_backoff(chunk)
        if llm_result is None:
            return None

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
        self, items: List[tuple[str, List[str]]],
    ) -> Dict[str, Dict[str, str]] | None:
        """Call ``_call_api_batch`` with exponential backoff retry.

        The pool handles provider-level failover internally.
        This method retries on transient (non-quota) errors.
        Returns the LLM readings dict, or ``None`` if all retries exhausted.
        """
        last_error: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._call_api_batch(items)
            except APIPoolExhaustedError:
                # All providers exhausted → no point retrying
                self._log("All API providers exhausted – batch FAILED", "error")
                with self._lock:
                    self.errors.append(
                        f"{datetime.now().isoformat()} | "
                        f"All API providers exhausted "
                        f"({len(items)} sentences skipped)"
                    )
                return None
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    self.retries += 1
                if attempt < self._max_retries:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    self._log(
                        f"Retry {attempt + 1}/{self._max_retries} in {wait}s "
                        f"– {last_error[:100]}", "warning"
                    )
                    time.sleep(wait)
                else:
                    self._log(
                        f"FAILED after {self._max_retries} retries – "
                        f"{last_error[:200]}", "error"
                    )
                    with self._lock:
                        self.errors.append(
                            f"{datetime.now().isoformat()} | "
                            f"{last_error}\n{traceback.format_exc()}"
                        )

        return None

    def _call_api(self, sentence: str) -> Dict[str, str]:
        """Single LLM API call for one sentence.

        Uses the unified ``llm_call`` method which handles all providers.
        """
        self._rate_limit()
        self.api_calls += 1

        system_prompt = (
            "You are a Japanese language expert specializing in furigana annotation. "
            "Your ONLY task is to return correct readings for kanji-containing "
            "substrings in a given sentence. Use the FULL sentence context to "
            "self-tokenize and choose the correct reading.\n\n"
            "RULES:\n"
            "1. For kanji-containing substrings → return HIRAGANA reading with okurigana "
            "(e.g. 食べる → たべる, 行った → いった)\n"
            "2. For KATAKANA loanwords → return ENGLISH meaning "
            "(e.g. コンピュータ → computer)\n"
            "3. If a substring belongs to a larger compound, return the full compound "
            "as the key, not the individual parts.\n"
            "4. Return ONLY a valid JSON object, no other text.\n\n"
            'Example: {"文庫本":"ぶんこぼん","毎日":"まいにち","日本語":"にほんご"}'
        )

        user_prompt = (
            f"Sentence: {sentence}\n\n"
            f"JSON:"
        )

        content = self._call_with_concurrency_limit(
            self._pool.llm_call,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self._model,
        )

        parsed = _extract_json(content)

        # Validate: values must be strings
        return {str(k): str(v) for k, v in parsed.items() if v}

    def _call_api_batch(
        self, items: List[tuple[str, List[str]]],
    ) -> Dict[str, Dict[str, str]]:
        """One LLM API call for MANY sentences.  Thread-safe.

        Sends sentences only.  The LLM self-tokenizes each sentence and
        annotates the correct substrings with readings.

        Uses the unified ``llm_call`` method with automatic provider failover.
        """
        with self._lock:
            self._rate_limit()
            self.api_calls += 1

        input_json = json.dumps(
            [sentence for sentence, _words in items],
            ensure_ascii=False,
        )

        system_prompt = (
            "You are a Japanese language expert specializing in furigana annotation. "
            "Your task is to provide the CORRECT reading for every kanji-containing substring "
            "that should receive ruby annotation in each sentence below, using the FULL "
            "sentence context to disambiguate.\n\n"
            "RULES:\n"
            "1. Return readings for every kanji-containing substring as a JSON object.\n"
            "2. If a substring belongs to a larger compound, return the full compound "
            "as the key instead of the individual pieces.\n"
            "3. For kanji words → return HIRAGANA reading with okurigana "
            "(e.g. 食べる→たべる, 行った→いった)\n"
            "4. For KATAKANA loanwords → return ENGLISH meaning "
            "(e.g. コンピュータ→computer)\n"
            "5. Use the EXACT sentence text as the top-level JSON key.\n"
            "6. Return ONLY a valid JSON object, no other text.\n\n"
            'Example: {"私は毎日日本語を勉強します":{"日本語":"にほんご","勉強":"べんきょう"}}'
        )

        user_prompt = (
            f"Annotate ALL sentences below:\n\n"
            f"{input_json}\n\nJSON:"
        )

        content = self._call_with_concurrency_limit(
            self._pool.llm_call,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self._model,
        )

        parsed = _extract_json(content)

        result: Dict[str, Dict[str, str]] = {}
        for sent, word_dict in parsed.items():
            if isinstance(word_dict, dict):
                result[str(sent)] = {
                    str(k): str(v) for k, v in word_dict.items() if v
                }

        if not result:
            self._log(
                f"LLM returned empty/invalid result. "
                f"Raw response (first 200 chars): {content[:200]!r}",
                "warning",
            )

        return result

    def _call_with_concurrency_limit(self, fn, **kwargs: Any) -> str:
        """Execute *fn(**kwargs)* respecting ``max_concurrent`` limit.

        Acquires the concurrency semaphore (if configured) before calling,
        releases after.  This limits simultaneous API calls across ALL
        threads and files.
        """
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

    def _evict_if_needed(self) -> None:
        """Simple FIFO eviction when cache exceeds max size."""
        if len(self._cache) >= self._cache_size:
            # Remove oldest entry (first key in insertion order, Python 3.7+)
            oldest = next(iter(self._cache))
            del self._cache[oldest]

    @property
    def stats(self) -> Dict[str, int]:
        """Return cache/API statistics."""
        return {
            "cache_size": len(self._cache),
            "cache_hits": self.cache_hits,
            "api_calls": self.api_calls,
            "retries": self.retries,
            "errors": len(self.errors),
            "total_annotated": self.total_annotated,
            "total_failed": self.total_failed,
        }
