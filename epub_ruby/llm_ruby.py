"""
LLM-based kanji reading using DeepSeek API for context-aware furigana.

Key insight: a kanji character can have multiple readings (onyomi/kunyomi),
and the correct reading can only be determined by putting the word back into
the original sentence.  This module uses DeepSeek to do exactly that.

Architecture:
  - fugashi (UniDic) handles tokenization → reliable word boundaries
  - DeepSeek LLM provides context-aware readings for each kanji word
  - Katakana loanwords still use fugashi's lemma (English gloss)
  - Built-in LRU cache + rate limiting to avoid redundant API calls
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

from openai import OpenAI

# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

_JSON_PATTERN = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _extract_json(text: str) -> Dict[str, Any]:
    """Try to pull a JSON object out of an LLM response string."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        pass

    # Try to find a {...} block
    for match in _JSON_PATTERN.finditer(text):
        try:
            return json.loads(match.group())  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue

    return {}


# ---------------------------------------------------------------------------
# LLMRubyReader
# ---------------------------------------------------------------------------


class LLMRubyReader:
    """Query DeepSeek API for context-aware kanji readings.

    Usage::

        reader = LLMRubyReader(api_key="sk-...")
        readings = reader.get_readings("私は毎日日本語を勉強します",
                                       ["私", "毎日", "日本語", "勉強"])
        # → {"私": "わたし", "毎日": "まいにち",
        #     "日本語": "にほんご", "勉強": "べんきょう"}
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        cache_size: int = 8192,
        rate_limit_delay: float = 0.1,
        timeout: float = 120.0,
        max_retries: int = 3,
    ) -> None:
        """Create a new LLM-based ruby reader.

        Args:
            api_key: DeepSeek API key. Defaults to ``DEEPSEEK_API_KEY`` env var.
            model: Model name. Use ``deepseek-v4-flash`` for speed/cost or
                   ``deepseek-v4-pro`` for quality.
            base_url: API base URL.
            cache_size: Max number of cached sentence→readings mappings.
            rate_limit_delay: Seconds to wait between API calls (default 0.1s).
            timeout: HTTP request timeout in seconds (default 120s).
            max_retries: Max retries per failed API call (default 3).
        """
        key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise ValueError(
                "DeepSeek API key not found. Set DEEPSEEK_API_KEY environment "
                "variable or pass api_key=..."
            )

        self._client = OpenAI(api_key=key, base_url=base_url, timeout=timeout,
                              max_retries=0)  # we handle retries ourselves
        self._model = model
        self._cache: Dict[str, Dict[str, str]] = {}
        self._cache_size = cache_size
        self._rate_limit_delay = rate_limit_delay
        self._last_call_time = 0.0
        self._max_retries = max_retries
        self._lock = threading.Lock()

        # Statistics
        self.cache_hits = 0
        self.api_calls = 0
        self.retries = 0
        self.errors: List[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _log(msg: str) -> None:
        """Timestamped log to stderr."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [LLM {ts}] {msg}", file=sys.stderr, flush=True)

    def get_readings(self, sentence: str, words: List[str]) -> Dict[str, str]:
        """Get context-aware readings for *words* inside *sentence*.

        Returns a ``{word: reading}`` dict.  Words not recognized by the LLM
        are simply omitted from the result (caller should fall back to fugashi).

        .. note::
            Prefer :meth:`get_readings_batch` for processing multiple
            sentences at once – it makes ONE API call instead of N.
        """
        if not words:
            return {}

        # Deduplicate while preserving order
        unique_words = list(dict.fromkeys(words))

        # Check cache
        cache_key = sentence
        if cache_key in self._cache:
            self.cache_hits += 1
            cached = self._cache[cache_key]
            return {w: cached[w] for w in unique_words if w in cached}

        # Build prompt & call API
        try:
            result = self._call_api(sentence, unique_words)
        except Exception as exc:
            print(f"  [LLM] API error: {exc}")
            return {}

        # Cache
        self._evict_if_needed()
        self._cache[cache_key] = result

        return {w: result[w] for w in unique_words if w in result}

    # ------------------------------------------------------------------
    # Batch API (one call per file, NOT per sentence)
    # ------------------------------------------------------------------

    # Max sentences per batch (smaller = faster per-call LLM response).
    _MAX_BATCH_SENTENCES = 60

    def get_readings_batch(
        self, items: List[tuple[str, Dict[str, str]]],
    ) -> Dict[str, Dict[str, str]]:
        """Get readings for MANY sentences in a single API call.

        Args:
            items: List of ``(sentence, {word: fugashi_reading})`` tuples.
                   Fugashi readings are sent to the LLM as hints; the LLM
                   only returns corrections for words where fugashi is wrong.

        Returns:
            ``{sentence: {word: reading}}`` — FULL readings (fugashi base
            merged with LLM corrections).  Words the LLM cannot read use
            the fugashi reading as-is.
        """
        if not items:
            return {}

        # Deduplicate sentences, merge word sets
        seen: Dict[str, Dict[str, str]] = {}
        for sentence, word_dict in items:
            if sentence not in seen:
                seen[sentence] = dict(word_dict)
            else:
                seen[sentence].update(word_dict)

        result: Dict[str, Dict[str, str]] = {}

        # Check cache first → cached value is the FULL merged reading
        uncached: List[tuple[str, Dict[str, str]]] = []
        for sentence, word_dict in seen.items():
            if sentence in self._cache:
                self.cache_hits += 1
                cached = self._cache[sentence]
                result[sentence] = {w: cached.get(w, word_dict.get(w, ""))
                                    for w in word_dict}
            else:
                uncached.append((sentence, word_dict))

        if not uncached:
            return result

        # Build chunks
        chunks: List[tuple[int, List[tuple[str, Dict[str, str]]]]] = []
        total_chunks = (len(uncached) + self._MAX_BATCH_SENTENCES - 1) // self._MAX_BATCH_SENTENCES
        for i in range(0, len(uncached), self._MAX_BATCH_SENTENCES):
            chunk = uncached[i : i + self._MAX_BATCH_SENTENCES]
            chunk_num = i // self._MAX_BATCH_SENTENCES + 1
            chunks.append((chunk_num, chunk))

        self._log(
            f"Dispatching {total_chunks} batch(es) × ~{self._MAX_BATCH_SENTENCES} "
            f"sentences – all in parallel"
        )

        # Process ALL chunks in parallel
        workers = total_chunks if total_chunks else 1
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
                    self._log(f"Batch {chunk_num} – unexpected error: {exc}")
                    batch_result = None

                if batch_result is None:
                    # Failed → find the chunk and use fugashi fallback
                    for cn, ch in chunks:
                        if cn == chunk_num:
                            for sentence, word_dict in ch:
                                result[sentence] = dict(word_dict)
                            break
                    continue

                # Merge into final result
                for sentence, merged in batch_result.items():
                    self._evict_if_needed()
                    self._cache[sentence] = merged
                    result[sentence] = merged

        return result

    def _process_batch(
        self, chunk_num: int,
        chunk: List[tuple[str, Dict[str, str]]],
        total_chunks: int,
    ) -> Dict[str, Dict[str, str]] | None:
        """Process one batch: call LLM, merge with fugashi.  Thread-safe."""
        self._log(
            f"Batch {chunk_num}/{total_chunks} – "
            f"{len(chunk)} sentences, "
            f"{sum(len(w) for _, w in chunk)} words – calling API..."
        )

        chunk_corrections = self._retry_with_backoff(chunk)
        if chunk_corrections is None:
            return None

        corrections_count = sum(len(c) for c in chunk_corrections.values())
        self._log(
            f"Batch {chunk_num}/{total_chunks} – done "
            f"({corrections_count} corrections)"
        )

        # Merge: fugashi base + LLM corrections
        result: Dict[str, Dict[str, str]] = {}
        for sentence, word_dict in chunk:
            corrections = chunk_corrections.get(sentence, {})
            merged = dict(word_dict)
            merged.update(corrections)
            result[sentence] = merged

        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _retry_with_backoff(
        self, items: List[tuple[str, Dict[str, str]]],
    ) -> Dict[str, Dict[str, str]] | None:
        """Call ``_call_api_batch`` with exponential backoff retry.

        Returns the corrections dict, or ``None`` if all retries exhausted.
        """
        last_error: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._call_api_batch(items)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    self.retries += 1
                if attempt < self._max_retries:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    self._log(
                        f"Retry {attempt + 1}/{self._max_retries} in {wait}s "
                        f"– {last_error[:100]}"
                    )
                    time.sleep(wait)
                else:
                    self._log(
                        f"FAILED after {self._max_retries} retries – "
                        f"{last_error[:200]}"
                    )
                    with self._lock:
                        self.errors.append(
                            f"{datetime.now().isoformat()} | "
                            f"{last_error}\n{traceback.format_exc()}"
                        )

        return None

    def _call_api(self, sentence: str, words: List[str]) -> Dict[str, str]:
        """Single DeepSeek API call for one sentence."""
        self._rate_limit()
        self.api_calls += 1

        words_block = "\n".join(f"  - {w}" for w in words)

        system_prompt = (
            "You are a Japanese language expert specializing in furigana annotation. "
            "Your ONLY task is to return correct readings for Japanese words "
            "based on the FULL sentence context.\n\n"
            "RULES:\n"
            "1. For words containing KANJI → return HIRAGANA reading (include "
            "okurigana: e.g. 食べる → たべる,  行った → いった)\n"
            "2. For KATAKANA loanwords → return ENGLISH meaning "
            "(e.g. コンピュータ → computer)\n"
            "3. Use sentence context to pick the CORRECT reading when a word "
            "has multiple possible readings\n"
            "4. Return ONLY a valid JSON object, no other text\n\n"
            'Example: {"毎日":"まいにち","日本語":"にほんご","勉強":"べんきょう"}'
        )

        user_prompt = (
            f"Sentence: {sentence}\n\n"
            f"Words to annotate:\n{words_block}\n\n"
            f"JSON:"
        )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            temperature=0.1,
        )

        content = response.choices[0].message.content or "{}"
        parsed = _extract_json(content)

        # Validate: values must be strings
        return {str(k): str(v) for k, v in parsed.items() if v}

    def _call_api_batch(
        self, items: List[tuple[str, Dict[str, str]]],
    ) -> Dict[str, Dict[str, str]]:
        """One DeepSeek API call for MANY sentences.  Thread-safe."""
        with self._lock:
            self._rate_limit()
            self.api_calls += 1

        input_json = json.dumps(
            [{"s": sentence, "w": word_dict}
             for sentence, word_dict in items],
            ensure_ascii=False,
        )

        system_prompt = (
            "You are a Japanese language expert. Your task is to SPOT-CHECK "
            "fugashi readings for accuracy using full sentence context.\n\n"
            "You will receive sentences with fugashi's suggested readings.\n"
            "ONLY return words where fugashi's reading is WRONG in context, "
            "with the CORRECTED reading.\n\n"
            "RULES:\n"
            "1. If all readings are correct → return {} for that sentence\n"
            "2. KANJI corrections → HIRAGANA with okurigana "
            "(e.g. 食べる→たべる)\n"
            "3. KATAKANA loanword corrections → ENGLISH "
            "(e.g. コンピュータ→computer)\n"
            "4. Use the EXACT sentence text as the key\n"
            "5. Return ONLY a valid JSON object, no other text\n\n"
            'Example: {"sentence text":{"私":"わたし"}} means only '
            '"私" needed fixing; all other words were correct.'
        )

        user_prompt = (
            f"Spot-check fugashi readings below. "
            f"Only return corrections:\n\n{input_json}\n\nJSON:"
        )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            temperature=0.1,
        )

        content = response.choices[0].message.content or "{}"
        parsed = _extract_json(content)

        result: Dict[str, Dict[str, str]] = {}
        for sent, word_dict in parsed.items():
            if isinstance(word_dict, dict):
                result[str(sent)] = {
                    str(k): str(v) for k, v in word_dict.items() if v
                }
        return result

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
        }
