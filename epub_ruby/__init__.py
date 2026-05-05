# Based on: https://github.com/yihong0618/epubhv
"""
epub-ruby — Add Japanese furigana/ruby annotations to EPUB books.

Supports:
  - Kanji -> hiragana reading
  - Katakana loanwords -> English reading (via lemma)
"""

from .core import EPUBHV, list_all_epub_in_dir

__all__ = ["EPUBHV", "list_all_epub_in_dir"]
