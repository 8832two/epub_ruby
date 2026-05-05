# Based on: https://github.com/yihong0618/epubhv
"""CLI for adding Japanese ruby/furigana annotations to EPUB files."""

from __future__ import annotations

from argparse import ArgumentParser, RawTextHelpFormatter
from pathlib import Path

from .core import EPUBHV, list_all_epub_in_dir


def main() -> None:
    """Parse command-line arguments and run the EPUB annotation pipeline."""
    parser = ArgumentParser(
        description="Add Japanese ruby/furigana annotations to EPUB files.",
        formatter_class=RawTextHelpFormatter,
    )
    parser.add_argument(
        "epub",
        help="EPUB file or directory containing EPUB files to annotate",
    )
    parser.add_argument(
        "-d", "--dest",
        default=".",
        type=Path,
        help="Destination directory (defaults to current directory)",
    )

    # ---- LLM options -------------------------------------------------------
    llm_group = parser.add_argument_group("LLM options (DeepSeek API)")
    llm_group.add_argument(
        "--use-llm",
        action="store_true",
        default=False,
        help="Use DeepSeek API for context-aware kanji readings",
    )
    llm_group.add_argument(
        "--llm-model",
        default="deepseek-v4-flash",
        choices=["deepseek-v4-pro", "deepseek-v4-flash"],
        help="DeepSeek model (default: deepseek-v4-flash)",
    )
    llm_group.add_argument(
        "--llm-api-key",
        default=None,
        help="DeepSeek API key (defaults to DEEPSEEK_API_KEY env var)",
    )
    llm_group.add_argument(
        "--llm-base-url",
        default="https://api.deepseek.com",
        help="API base URL (default: https://api.deepseek.com)",
    )

    args = parser.parse_args()
    epub_path = Path(args.epub)

    if epub_path.is_dir():
        files = sorted(list_all_epub_in_dir(epub_path))
        for f in files:
            print(f"Processing: {f}")
            EPUBHV(
                f,
                use_llm=args.use_llm,
                llm_model=args.llm_model,
                llm_api_key=args.llm_api_key,
                llm_base_url=args.llm_base_url,
            ).run(dest=args.dest)
        print(f"Done! Processed {len(files)} EPUB file(s).")
    else:
        print(f"Processing: {epub_path}")
        result = EPUBHV(
            epub_path,
            use_llm=args.use_llm,
            llm_model=args.llm_model,
            llm_api_key=args.llm_api_key,
            llm_base_url=args.llm_base_url,
        ).run(dest=args.dest)
        print(f"Done! Output: {result}")


if __name__ == "__main__":
    main()
