"""
Unified logging system for epub-ruby.

Provides structured logging with file and console handlers.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional
import sys


class ColoredFormatter(logging.Formatter):
    """Formatter with ANSI color codes for console output."""

    # ANSI color codes
    _COLORS = {
        "DEBUG": "\033[36m",    # Cyan
        "INFO": "\033[32m",     # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",    # Red
        "CRITICAL": "\033[41m", # Red background
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with colors."""
        if record.levelname in self._COLORS and sys.stderr.isatty():
            color = self._COLORS[record.levelname]
            record.levelname = f"{color}{record.levelname}{self._RESET}"
        return super().format(record)


def setup_logging(
    name: str = "epub_ruby",
    level: int = logging.INFO,
    log_dir: Optional[Path] = None,
    log_file: str = "epub_ruby.log",
    console: bool = True,
) -> logging.Logger:
    """Configure logging for the application.

    Args:
        name: Logger name
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_dir: Directory for log files. If None, uses ~/.epub_ruby/
        log_file: Log file name
        console: Whether to log to console

    Returns:
        Configured logger instance
    """
    # Set up log directory
    if log_dir is None:
        log_dir = Path.home() / ".epub_ruby"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Get or create logger
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    logger.handlers = []

    # Format
    detailed_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler (rotate by file size)
    log_file_path = log_dir / log_file
    file_handler = logging.handlers.RotatingFileHandler(
        log_file_path,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(detailed_fmt)
    logger.addHandler(file_handler)

    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(ColoredFormatter("%(levelname)s: %(message)s"))
        logger.addHandler(console_handler)

    return logger


# Default logger
_logger: Optional[logging.Logger] = None


def get_logger(name: str = "epub_ruby") -> logging.Logger:
    """Get the default logger."""
    global _logger
    if _logger is None:
        _logger = setup_logging(name)
    return _logger


def debug(msg: str, *args, **kwargs) -> None:
    """Log debug message."""
    get_logger().debug(msg, *args, **kwargs)


def info(msg: str, *args, **kwargs) -> None:
    """Log info message."""
    get_logger().info(msg, *args, **kwargs)


def warning(msg: str, *args, **kwargs) -> None:
    """Log warning message."""
    get_logger().warning(msg, *args, **kwargs)


def error(msg: str, *args, **kwargs) -> None:
    """Log error message."""
    get_logger().error(msg, *args, **kwargs)


def critical(msg: str, *args, **kwargs) -> None:
    """Log critical message."""
    get_logger().critical(msg, *args, **kwargs)
