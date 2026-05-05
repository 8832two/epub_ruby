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

    args = parser.parse_args()
    epub_path = Path(args.epub)

    if epub_path.is_dir():
        files = sorted(list_all_epub_in_dir(epub_path))
        for f in files:
            print(f"Processing: {f}")
            EPUBHV(f).run(dest=args.dest)
        print(f"Done! Processed {len(files)} EPUB file(s).")
    else:
        print(f"Processing: {epub_path}")
        result = EPUBHV(epub_path).run(dest=args.dest)
        print(f"Done! Output: {result}")


if __name__ == "__main__":
    main()
