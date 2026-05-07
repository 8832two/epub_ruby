"""
epub-ruby — 为 EPUB 电子书添加日语振假名（furigana/ruby）注音。

支持：
  - 基于 fugashi (UniDic) 的汉字→平假名读音
  - 片假名外来语→英文释义（通过词条）
  - 基于 LLM（DeepSeek / OpenAI / Gemini）的上下文感知汉字读音
  - 多 API 提供商自动故障切换
  - PySide6 图形界面 (GUI)
"""

__version__ = "0.3.0"

from .core import EPUBHV, list_all_epub_in_dir

__all__ = ["EPUBHV", "list_all_epub_in_dir", "__version__"]
