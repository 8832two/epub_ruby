"""
命令行接口 — 为 EPUB 文件添加日语振假名注音。

支持：
  - 传统 fugashi 词典模式
  - LLM 上下文感知模式（--use-llm）
  - 多 API 提供商池（--api 可多次指定，自动故障切换）
"""

from __future__ import annotations

from argparse import ArgumentParser, RawTextHelpFormatter
from pathlib import Path

from .core import EPUBHV, list_all_epub_in_dir
from .api_pool import APIPool, APIConfig


def _parse_api_spec(spec: str) -> APIConfig:
    """Parse an API spec string: ``provider:api_key:model:base_url``.

    All fields except ``provider`` are optional (empty = use default/env var).
    """
    parts = spec.split(":", 3)
    if len(parts) < 1 or not parts[0]:
        raise ValueError(f"Invalid API spec: {spec!r}  Expected format: provider:api_key:model:base_url")

    provider = parts[0].strip()
    api_key = parts[1].strip() if len(parts) > 1 else ""
    model = parts[2].strip() if len(parts) > 2 else ""
    base_url = parts[3].strip() if len(parts) > 3 else ""

    return APIConfig(provider=provider, api_key=api_key, model=model, base_url=base_url)


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
    llm_group = parser.add_argument_group("LLM options")
    llm_group.add_argument(
        "--use-llm",
        action="store_true",
        default=False,
        help="Use LLM API for context-aware kanji readings",
    )

    # Multi-API pool (new)
    llm_group.add_argument(
        "--api",
        action="append",
        dest="api_specs",
        default=[],
        metavar="provider:key:model:url",
        help=(
            "Add an API provider to the pool.\n"
            "Format: provider:api_key:model:base_url\n"
            "Can be specified multiple times for failover.\n"
            "Providers: deepseek, openai, gemini\n"
            "Examples:\n"
            '  --api "deepseek:sk-xxx:deepseek-v4-flash:"\n'
            '  --api "gemini::gemini-3-flash-preview:"'
        ),
    )

    # Legacy single-provider options (backward-compatible)
    llm_group.add_argument(
        "--llm-model",
        default=None,
        help="Model name (legacy; prefer --api)",
    )
    llm_group.add_argument(
        "--llm-api-key",
        default=None,
        help="API key (legacy; prefer --api)",
    )
    llm_group.add_argument(
        "--llm-base-url",
        default=None,
        help="API base URL (legacy; prefer --api)",
    )

    # Batch size
    llm_group.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Max sentences per API call (default: 200). Larger = fewer API calls = less token waste.",
    )

    # Concurrency limit
    llm_group.add_argument(
        "--max-concurrent",
        type=int,
        default=0,
        help=(
            "Max simultaneous API calls (default: 0 = unlimited). "
            "Set to 5-10 for Gemini free tier (15 RPM). "
            "Applies across ALL files and batches."
        ),
    )

    args = parser.parse_args()
    epub_path = Path(args.epub)

    # Build API pool
    llm_pool = None
    if args.use_llm:
        if args.api_specs:
            configs = [_parse_api_spec(s) for s in args.api_specs]
            llm_pool = APIPool(configs)
            print(f"[Pool] {len(configs)} API provider(s) configured")
            for i, cfg in enumerate(configs):
                print(f"  [{i+1}] {cfg.provider}: {cfg.model}")

    # Build kwargs for EPUBHV
    epub_kwargs = {
        "use_llm": args.use_llm,
        "llm_batch_size": args.batch_size,
        "llm_max_concurrent": args.max_concurrent,
        "llm_pool": llm_pool,
    }

    # Legacy LLM options (used when --api is not specified)
    if args.llm_model is not None:
        epub_kwargs["llm_model"] = args.llm_model
    if args.llm_api_key is not None:
        epub_kwargs["llm_api_key"] = args.llm_api_key
    if args.llm_base_url is not None:
        epub_kwargs["llm_base_url"] = args.llm_base_url

    if epub_path.is_dir():
        files = sorted(list_all_epub_in_dir(epub_path))
        for f in files:
            print(f"Processing: {f}")
            EPUBHV(f, **epub_kwargs).run(dest=args.dest)
        print(f"Done! Processed {len(files)} EPUB file(s).")
    else:
        print(f"Processing: {epub_path}")
        result = EPUBHV(epub_path, **epub_kwargs).run(dest=args.dest)
        print(f"Done! Output: {result}")


if __name__ == "__main__":
    main()
