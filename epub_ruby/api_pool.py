"""
Multi-provider API pool with automatic failover on quota exhaustion.

Supports:
  - DeepSeek (OpenAI-compatible)
  - OpenAI / OpenAI-compatible endpoints
  - Gemini (google-genai)

Usage::

    pool = APIPool([
        APIConfig(provider="deepseek", api_key="sk-...", model="deepseek-v4-flash"),
        APIConfig(provider="gemini", api_key="...", model="gemini-3-flash-preview"),
    ])

    # The pool auto-switches to the next available API on quota errors.
    response = pool.chat_completion(messages=[...])
"""

from __future__ import annotations

import os
import sys
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Provider types
# ---------------------------------------------------------------------------

class Provider(Enum):
    """Supported API providers."""
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    GEMINI = "gemini"


# Default base URLs for known providers
_DEFAULT_BASE_URLS: Dict[Provider, str] = {
    Provider.DEEPSEEK: "https://api.deepseek.com",
    Provider.OPENAI: "https://api.openai.com/v1",
    Provider.GEMINI: "",  # Gemini uses SDK, not a base URL
}

# Default env var names for API keys
_DEFAULT_KEY_ENVVARS: Dict[Provider, str] = {
    Provider.DEEPSEEK: "DEEPSEEK_API_KEY",
    Provider.OPENAI: "OPENAI_API_KEY",
    Provider.GEMINI: "GEMINI_API_KEY",
}


# ---------------------------------------------------------------------------
# API configuration
# ---------------------------------------------------------------------------


@dataclass
class APIConfig:
    """Configuration for one API endpoint.

    Args:
        provider: Provider type (``"deepseek"``, ``"openai"``, ``"gemini"``).
        api_key: API key. If empty, reads from the provider's env var.
        model: Model name.
        base_url: Override base URL (OpenAI-compatible providers only).
    """
    provider: str
    api_key: str = ""
    model: str = ""
    base_url: str = ""

    def __post_init__(self) -> None:
        provider_enum = Provider(self.provider)

        # Resolve API key from env var if not provided
        if not self.api_key:
            env_var = _DEFAULT_KEY_ENVVARS.get(provider_enum, "")
            self.api_key = os.environ.get(env_var, "")

        # Resolve base URL default
        if not self.base_url:
            self.base_url = _DEFAULT_BASE_URLS.get(provider_enum, "")


# ---------------------------------------------------------------------------
# Quota / rate-limit error detection
# ---------------------------------------------------------------------------

# HTTP status codes that indicate quota/rate-limit exhaustion
_QUOTA_STATUS_CODES = {429, 402, 403, 503}

# Substrings in error messages that suggest quota issues
_QUOTA_MESSAGE_PATTERNS = [
    "quota", "rate limit", "rate exceeded", "insufficient",
    "billing", "exceeded your current quota", "resource exhausted",
    "too many requests", "payment required", "free tier",
    "daily limit", "rpm", "tpm", "rpd",
    "RESOURCE_EXHAUSTED", "429", "capacity",
]


def _is_quota_error(error: Exception) -> bool:
    """Heuristic: is this error likely caused by quota/rate-limit exhaustion?"""
    msg = str(error).lower()
    for pattern in _QUOTA_MESSAGE_PATTERNS:
        if pattern.lower() in msg:
            return True

    # Check for HTTP status codes in error attributes
    for attr in ("status_code", "code", "http_status", "status"):
        val = getattr(error, attr, None)
        if val in _QUOTA_STATUS_CODES:
            return True

    # OpenAI SDK errors often have http_status
    http_status = getattr(error, "http_status", None)
    if http_status in _QUOTA_STATUS_CODES:
        return True

    # Check nested __cause__
    cause = getattr(error, "__cause__", None)
    if cause is not None:
        return _is_quota_error(cause)

    return False


# HTTP status codes that indicate a *permanent* failure (retrying won't help)
_PERMANENT_STATUS_CODES = {404, 401, 400}

# Substrings in error messages that suggest a permanent failure
_PERMANENT_MESSAGE_PATTERNS = [
    "not found", "NOT_FOUND", "not supported",
    "invalid api key", "invalid_api_key", "unauthorized",
    "permission denied", "access denied",
    "model is not", "does not exist",
    "bad request", "invalid model",
]


def _is_permanent_error(error: Exception) -> bool:
    """Heuristic: is this error a *permanent* failure (retrying won't help)?

    Permanent errors include: model not found (404), invalid API key (401),
    bad request (400).  These should stop the entire operation immediately.
    """
    msg = str(error)
    for pattern in _PERMANENT_MESSAGE_PATTERNS:
        if pattern.lower() in msg.lower():
            return True

    # Check for HTTP status codes
    for attr in ("status_code", "code", "http_status", "status"):
        val = getattr(error, attr, None)
        if val in _PERMANENT_STATUS_CODES:
            return True

    http_status = getattr(error, "http_status", None)
    if http_status in _PERMANENT_STATUS_CODES:
        return True

    # Check nested __cause__
    cause = getattr(error, "__cause__", None)
    if cause is not None:
        return _is_permanent_error(cause)

    return False


# ---------------------------------------------------------------------------
# Pool statistics
# ---------------------------------------------------------------------------


@dataclass
class PoolStats:
    """Runtime statistics for the API pool."""
    total_calls: int = 0
    total_retries: int = 0
    total_failovers: int = 0
    errors: List[str] = field(default_factory=list)
    # Per-provider tracking
    provider_calls: Dict[str, int] = field(default_factory=dict)
    provider_errors: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# API Pool
# ---------------------------------------------------------------------------


class APIQuotaExhaustedError(Exception):
    """Raised internally when a provider's quota is exhausted."""


class APIPoolExhaustedError(Exception):
    """Raised when ALL providers in the pool have been exhausted.

    Attributes:
        earliest_resume: Earliest time (Unix timestamp) when any paused
            provider will become available again, or 0 if unknown.
    """

    def __init__(self, *args: Any, earliest_resume: float = 0.0) -> None:
        super().__init__(*args)
        self.earliest_resume = earliest_resume


class APIPool:
    """Manage multiple API providers with automatic failover.

    Tries providers in order.  When one returns a quota/rate-limit error,
    it is paused and the next provider is tried.  Paused providers are
    periodically retried.

    Usage::

        pool = APIPool([
            APIConfig(provider="deepseek", api_key="sk-...", model="deepseek-v4-flash"),
            APIConfig(provider="gemini", api_key="...", model="gemini-3-flash-preview"),
        ])

        # Chat completion (OpenAI-compatible format)
        resp = pool.chat_completion(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.1,
        )

        # Gemini-style generate_content
        resp = pool.generate_content(
            model="gemini-3-flash-preview",
            contents="Explain AI",
        )
    """

    def __init__(
        self,
        configs: List[APIConfig],
        timeout: float = 120.0,
        pause_duration: float = 60.0,
    ) -> None:
        """
        Args:
            configs: List of API configurations (in priority order).
            timeout: HTTP request timeout in seconds.
            pause_duration: How long (seconds) to pause a provider after
                a quota error before retrying it.
        """
        if not configs:
            raise ValueError("APIPool requires at least one APIConfig")

        self._configs = configs
        self._timeout = timeout
        self._pause_duration = pause_duration

        # Runtime state
        self._current_index = 0
        self._paused_until: Dict[int, float] = {}  # config_index -> resume_time
        self._lock = threading.Lock()
        self._stats = PoolStats()

        # Permanent failure: when a provider returns a non-retryable error
        # (e.g. 404 model not found), the entire pool is marked dead so
        # subsequent calls fail immediately instead of wasting time.
        self._dead: bool = False
        self._dead_reason: str = ""

        # Lazy-initialized clients
        self._openai_clients: Dict[int, Any] = {}   # config_index -> OpenAI client
        self._gemini_clients: Dict[int, Any] = {}   # config_index -> genai.Client

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def stats(self) -> PoolStats:
        """Current pool statistics."""
        return self._stats

    @property
    def current_config(self) -> APIConfig:
        """The currently active API configuration."""
        with self._lock:
            idx = self._get_active_index()
            return self._configs[idx]

    @property
    def provider_count(self) -> int:
        """Number of providers in the pool."""
        return len(self._configs)

    @property
    def dead(self) -> bool:
        """``True`` when the pool has suffered a permanent, non-retryable error.

        Examples: model not found (404), invalid API key (401).
        When dead, all subsequent API calls fail immediately.
        """
        return self._dead

    @property
    def dead_reason(self) -> str:
        """Human-readable reason why the pool is dead (empty if alive)."""
        return self._dead_reason

    @property
    def all_paused(self) -> bool:
        """``True`` when every provider is currently paused (quota/rate-limit)."""
        with self._lock:
            now = time.time()
            return all(
                self._paused_until.get(i, 0) > now
                for i in range(len(self._configs))
            )

    @property
    def earliest_resume_time(self) -> float:
        """Earliest Unix timestamp when any paused provider will be available.

        Returns 0 if no providers are paused.
        """
        with self._lock:
            now = time.time()
            paused_times = [
                self._paused_until.get(i, 0)
                for i in range(len(self._configs))
                if self._paused_until.get(i, 0) > now
            ]
            return min(paused_times) if paused_times else 0.0

    # ------------------------------------------------------------------
    # High-level API methods
    # ------------------------------------------------------------------

    def llm_call(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = "",
        temperature: float = 0.1,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Unified LLM call – works across all providers.

        For OpenAI-compatible providers, uses chat completion with
        system + user messages.  For Gemini, merges prompts into a
        single content string with system instruction.

        Args:
            response_format: OpenAI-compatible response format dict
                (e.g. ``{"type": "json_object"}``).  Ignored by Gemini.

        Returns the text content.  Auto-fails over on quota errors.
        """
        return self._call_with_failover(
            lambda cfg, client: self._do_llm_call(
                cfg, client, system_prompt, user_prompt,
                model or cfg.model, temperature, response_format,
            )
        )

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.1,
        **kwargs: Any,
    ) -> str:
        """OpenAI-compatible chat completion.  Returns the text content.

        Automatically fails over to the next provider on quota errors.
        """
        return self._call_with_failover(
            lambda cfg, client: self._do_chat_completion(
                client, messages, model or cfg.model, temperature,
                **kwargs,
                **(
                    {"extra_body": {"thinking": {"type": "disabled"}}}
                    if Provider(cfg.provider) == Provider.DEEPSEEK
                    and "extra_body" not in kwargs
                    else {}
                ),
            )
        )

    def generate_content(
        self,
        contents: str,
        model: str = "",
        **kwargs: Any,
    ) -> str:
        """Gemini-style content generation.  Returns the text content.

        Only works when the active provider is Gemini.
        """
        return self._call_with_failover(
            lambda cfg, client: self._do_generate_content(
                client, contents, model or cfg.model, **kwargs
            )
        )

    # ------------------------------------------------------------------
    # Core failover logic
    # ------------------------------------------------------------------

    def _call_with_failover(
        self,
        fn: Any,
    ) -> str:
        """Execute *fn(config, client)* with automatic failover.

        *fn* receives ``(APIConfig, client)`` and must return ``str``.
        On quota errors, the current provider is paused and the next is tried.

        On *permanent* errors (404 model not found, 401 auth, etc.), the
        pool is marked dead and all subsequent calls fail immediately.
        """
        # ── Fast path: pool is dead from a previous permanent error ──
        if self._dead:
            raise APIPoolExhaustedError(
                f"Pool is dead: {self._dead_reason}"
            )

        last_error: Optional[Exception] = None

        with self._lock:
            self._stats.total_calls += 1

        # Try each provider in order (wrapping around if needed)
        n = len(self._configs)
        for attempt in range(n * 2):  # at most 2 full cycles
            idx = self._get_active_index()
            cfg = self._configs[idx]

            # Check if this provider is paused
            with self._lock:
                resume_time = self._paused_until.get(idx, 0)
            if resume_time > time.time():
                # Paused → skip to next
                self._advance_index()
                continue

            # Get or create client
            try:
                client = self._get_client(idx, cfg)
            except Exception as exc:
                self._log(f"[{cfg.provider}] Client init failed: {exc}")
                if _is_permanent_error(exc):
                    self._dead = True
                    self._dead_reason = f"{cfg.provider}: {exc}"
                    break
                self._pause_provider(idx)
                self._advance_index()
                last_error = exc
                continue

            # Execute
            try:
                result = fn(cfg, client)
                with self._lock:
                    self._stats.provider_calls[cfg.provider] = \
                        self._stats.provider_calls.get(cfg.provider, 0) + 1
                return result
            except APIQuotaExhaustedError:
                self._log(f"[{cfg.provider}] Quota exhausted, switching...")
                self._pause_provider(idx)
                self._advance_index()
                with self._lock:
                    self._stats.total_failovers += 1
                    self._stats.provider_errors[cfg.provider] = \
                        self._stats.provider_errors.get(cfg.provider, 0) + 1
                last_error = Exception(f"{cfg.provider}: quota exhausted")
            except Exception as exc:
                self._log(f"[{cfg.provider}] Error: {exc}")
                with self._lock:
                    self._stats.total_retries += 1
                    self._stats.provider_errors[cfg.provider] = \
                        self._stats.provider_errors.get(cfg.provider, 0) + 1

                # Permanent error → kill the pool immediately
                if _is_permanent_error(exc):
                    self._dead = True
                    self._dead_reason = f"{cfg.provider}: {exc}"
                    last_error = exc
                    break

                # Transient error → try next provider
                self._advance_index()
                last_error = exc

        # All providers exhausted (or pool dead)
        if self._dead:
            reason = self._dead_reason
            with self._lock:
                self._stats.errors.append(
                    f"{datetime.now().isoformat()} | Pool dead: {reason}"
                )
            raise APIPoolExhaustedError(
                f"Pool dead (permanent error): {reason}",
                earliest_resume=0,
            )

        earliest = self.earliest_resume_time
        with self._lock:
            self._stats.errors.append(
                f"{datetime.now().isoformat()} | All providers exhausted. "
                f"Last error: {last_error}"
            )
        raise APIPoolExhaustedError(
            f"All {n} API provider(s) exhausted. Last error: {last_error}",
            earliest_resume=earliest,
        )

    # ------------------------------------------------------------------
    # Provider-specific API calls
    # ------------------------------------------------------------------

    def _do_llm_call(
        self,
        cfg: APIConfig,
        client: Any,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Execute an LLM call, adapting to the provider type."""
        provider = Provider(cfg.provider)

        if provider in (Provider.DEEPSEEK, Provider.OPENAI):
            kwargs: Dict[str, Any] = {}
            if response_format is not None:
                kwargs["response_format"] = response_format
            # DeepSeek: disable thinking/reasoning to reduce latency and token cost
            if provider == Provider.DEEPSEEK:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            return self._do_chat_completion(
                client,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                temperature=temperature,
                **kwargs,
            )
        elif provider == Provider.GEMINI:
            # Merge system + user into single contents, use system_instruction
            return self._do_generate_content(
                client,
                contents=user_prompt,
                model=model,
                config={
                    "system_instruction": system_prompt,
                    "temperature": temperature,
                },
            )
        else:
            raise ValueError(f"Unknown provider: {cfg.provider}")

    def _do_chat_completion(
        self,
        client: Any,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float,
        **kwargs: Any,
    ) -> str:
        """Execute OpenAI-compatible chat completion."""
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                stream=False,
                **kwargs,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            if _is_quota_error(exc):
                raise APIQuotaExhaustedError(str(exc)) from exc
            raise

    def _do_generate_content(
        self,
        client: Any,
        contents: str,
        model: str,
        **kwargs: Any,
    ) -> str:
        """Execute Gemini content generation."""
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                **kwargs,
            )
            return response.text or ""
        except Exception as exc:
            if _is_quota_error(exc):
                raise APIQuotaExhaustedError(str(exc)) from exc
            raise

    # ------------------------------------------------------------------
    # Client initialization (lazy)
    # ------------------------------------------------------------------

    def _get_client(self, idx: int, cfg: APIConfig) -> Any:
        """Get or create an API client for the given config."""
        provider = Provider(cfg.provider)

        if provider in (Provider.DEEPSEEK, Provider.OPENAI):
            if idx not in self._openai_clients:
                from openai import OpenAI
                self._openai_clients[idx] = OpenAI(
                    api_key=cfg.api_key,
                    base_url=cfg.base_url,
                    timeout=self._timeout,
                    max_retries=0,
                )
            return self._openai_clients[idx]

        elif provider == Provider.GEMINI:
            if idx not in self._gemini_clients:
                from google import genai
                self._gemini_clients[idx] = genai.Client(
                    api_key=cfg.api_key or None,
                )
            return self._gemini_clients[idx]

        else:
            raise ValueError(f"Unknown provider: {cfg.provider}")

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _get_active_index(self) -> int:
        """Return the current active config index (thread-safe)."""
        with self._lock:
            return self._current_index

    def _advance_index(self) -> None:
        """Move to the next provider (circular)."""
        with self._lock:
            self._current_index = (self._current_index + 1) % len(self._configs)

    def _pause_provider(self, idx: int) -> None:
        """Pause a provider for ``_pause_duration`` seconds."""
        with self._lock:
            self._paused_until[idx] = time.time() + self._pause_duration

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        """Timestamped log to stderr."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [Pool {ts}] {msg}", file=sys.stderr, flush=True)
