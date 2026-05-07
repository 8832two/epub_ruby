"""
片假名 → 英文释义 独立工具
==========================

从日语文本中提取片假名外来语，并利用 fugashi (UniDic) 词条的 lemma 字段
标注对应的英文释义。

例如：``コンピュータ`` → ``computer``

用法：
    # 作为模块
    from epub_ruby.katakana_english import extract_katakana_readings

    readings = extract_katakana_readings("コンピュータでゲームをする")
    # => {"コンピュータ": "computer", "ゲーム": "game"}

    # 作为命令行工具
    python -m epub_ruby.katakana_english "コンピュータでゲームをする"
    python -m epub_ruby.katakana_english --file input.txt
    echo "テレビを見る" | python -m epub_ruby.katakana_english --stdin

依赖：
    - fugashi (pip install fugashi)
    - unidic (python -m unidic download)
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from itertools import groupby
from typing import Dict, Iterator, List, Tuple

# ---------------------------------------------------------------------------
# 片假名 ↔ 平假名 转换表
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

# 提取纯英文（去除数字、符号等）
_ENGLISH_ONLY_RE = re.compile(r"[^a-zA-Z\s]")


def katakana_to_hiragana(text: str) -> str:
    """将片假名转换为平假名。"""
    return text.translate(_K2H)


def hiragana_to_katakana(text: str) -> str:
    """将平假名转换为片假名。"""
    return text.translate(_H2K)


# ---------------------------------------------------------------------------
# fugashi Tagger（延迟初始化）
# ---------------------------------------------------------------------------

_tagger = None


def _get_tagger():
    """获取全局 fugashi Tagger 实例（首次调用时初始化）。"""
    global _tagger
    if _tagger is None:
        from fugashi import Tagger

        _tagger = Tagger()
    return _tagger


# ---------------------------------------------------------------------------
# 片假名 → 英文 核心逻辑
# ---------------------------------------------------------------------------


def _extract_english_from_lemma(lemma: str) -> str | None:
    """从 UniDic lemma 字段中提取英文释义。

    UniDic 的 lemma 字段对片假名外来语包含形如
    ``コンピュータ-computer`` 的条目。
    此函数提取连字符后的英文部分。
    """
    if "-" not in lemma:
        return None
    english = _ENGLISH_ONLY_RE.sub("", lemma.split("-", 1)[1])
    return english.strip() if english.strip() else None


def extract_katakana_readings(text: str) -> Dict[str, str]:
    """从文本中提取所有片假名外来语及其英文释义。

    Args:
        text: 日语文本。

    Returns:
        字典，键为片假名单词，值为对应的英文释义。
        不包含汉字和纯平假名单词。

    Example:
        >>> extract_katakana_readings("コンピュータでゲームをする")
        {"コンピュータ": "computer", "ゲーム": "game"}
    """
    result: Dict[str, str] = {}
    tagger = _get_tagger()

    for word in tagger(text):
        surface = word.surface
        lemma = word.feature.lemma or ""

        english = _extract_english_from_lemma(lemma)
        if english:
            # 避免重复键（相同片假名出现多次时取首次的释义）
            if surface not in result:
                result[surface] = english

    return result


# ---------------------------------------------------------------------------
# 生成带注音的 token 流
# ---------------------------------------------------------------------------


def _split_tail(text: str, reading: str) -> Iterator[str | Tuple[str, str]]:
    """分离 text 和 reading 的共同尾部字符。

    如果最后一个字符相同，则把共同后缀单独作为纯文本，
    前面部分作为 (text, reading) 对。
    """
    if text[-1] == reading[-1]:
        for i in range(1, min(len(reading), len(text))):
            if text[-i - 1] != reading[-i - 1]:
                yield (text[:-i], reading[:-i])
                yield reading[-i:]
                break
        else:
            yield (text, reading)
    else:
        yield (text, reading)


def generate_katakana_readings(
    sentence: str,
) -> Iterator[str | Tuple[str, str]]:
    """对日语句子进行分词，为片假名外来语生成英文注音。

    汉字和平假名部分保持原样（不注音）。

    Yields:
        ``str``        – 不需要注音的文本段。
        ``(str, str)`` – ``(片假名文本, 英文释义)`` 对。
    """
    tagger = _get_tagger()
    for word in tagger(sentence):
        surface = word.surface
        lemma = word.feature.lemma or ""

        english = _extract_english_from_lemma(lemma)
        if english:
            yield from _split_tail(surface, english)
        else:
            yield surface


# ---------------------------------------------------------------------------
# 带注音的格式化输出
# ---------------------------------------------------------------------------


def annotate_text(
    text: str,
    style: str = "plain",
) -> str:
    """对文本中的片假名外来语添加英文注音。

    Args:
        text: 输入日语文本。
        style: 输出风格。
            - ``"plain"``  – ``コンピュータ(computer)``
            - ``"ruby"``   – ``<ruby>コンピュータ<rt>computer</rt></ruby>``
            - ``"mapping"`` – ``コンピュータ: computer`` (每行一个)

    Returns:
        注音后的文本。
    """
    if style == "mapping":
        readings = extract_katakana_readings(text)
        return "\n".join(f"{k}: {v}" for k, v in readings.items())

    parts: List[str] = []
    for seg in generate_katakana_readings(text):
        if isinstance(seg, str):
            parts.append(seg)
        else:
            word, english = seg
            if style == "ruby":
                parts.append(f"<ruby>{word}<rt>{english}</rt></ruby>")
            else:  # plain
                parts.append(f"{word}({english})")

    return "".join(parts)


# ---------------------------------------------------------------------------
# 命令行接口
# ---------------------------------------------------------------------------


def main() -> None:
    """命令行入口。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="为日语文本中的片假名外来语标注英文释义",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "text",
        nargs="?",
        help="要处理的日语文本",
    )
    parser.add_argument(
        "-f", "--file",
        type=str,
        default=None,
        help="从文件读取文本",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取文本",
    )
    parser.add_argument(
        "-s", "--style",
        choices=["plain", "ruby", "mapping"],
        default="plain",
        help="输出风格:\n"
             "  plain   – コンピュータ(computer)\n"
             "  ruby    – HTML <ruby> 标签\n"
             "  mapping – 单词: 释义 (每行一个)",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="同时显示汉字和纯假名的读音（mapping 模式下默认显示全部词条信息）",
    )

    args = parser.parse_args()

    # 获取输入文本
    if args.stdin:
        text = sys.stdin.read()
    elif args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    elif args.text:
        text = args.text
    else:
        parser.print_help()
        sys.exit(1)

    text = text.strip()
    if not text:
        print("错误：输入文本为空", file=sys.stderr)
        sys.exit(1)

    # 输出
    if args.show_all and args.style == "mapping":
        tagger = _get_tagger()
        for word in tagger(text):
            surface = word.surface
            kana = word.feature.kana
            lemma = word.feature.lemma or ""
            english = _extract_english_from_lemma(lemma)
            if english:
                print(f"{surface} (片假名) → {english}")
            elif surface != kana and kana not in (None, "", "*"):
                print(f"{surface} (汉字) → {katakana_to_hiragana(str(kana))}")
    else:
        print(annotate_text(text, style=args.style))


if __name__ == "__main__":
    main()
