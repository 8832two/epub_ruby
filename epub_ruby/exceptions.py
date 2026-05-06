"""
Custom exceptions for epub-ruby.
"""

from __future__ import annotations


class EPUBRubyError(Exception):
    """Base exception for epub-ruby errors."""
    pass


class EPUBError(EPUBRubyError):
    """Error related to EPUB file processing."""
    pass


class EPUBInvalidError(EPUBError):
    """EPUB file is invalid or corrupted."""
    pass


class EPUBExtractionError(EPUBError):
    """Failed to extract EPUB file."""
    pass


class EPUBPackingError(EPUBError):
    """Failed to re-pack EPUB file."""
    pass


class RubyError(EPUBRubyError):
    """Error related to ruby/furigana annotation."""
    pass


class LLMError(EPUBRubyError):
    """Error related to LLM API processing."""
    pass


class LLMAPIError(LLMError):
    """LLM API call failed."""
    pass


class LLMParseError(LLMError):
    """Failed to parse LLM response."""
    pass


class LLMBatchError(LLMError):
    """Batch API call failed."""
    pass


class APIPoolError(EPUBRubyError):
    """Error related to API pool management."""
    pass


class ConfigError(EPUBRubyError):
    """Configuration error."""
    pass
