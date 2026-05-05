# Based on: https://github.com/yihong0618/epubhv
"""
Add Japanese furigana/ruby annotations to EPUB books.

Supports:
  - Kanji -> hiragana reading
  - Katakana loanwords -> English reading
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Set

from bs4 import BeautifulSoup

from .ruby import RubySoup, string_containers

if TYPE_CHECKING:
    from .llm_ruby import LLMRubyReader


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def list_all_epub_in_dir(path: Path) -> Set[Path]:
    """Recursively find all .epub files under *path*."""
    return set(path.rglob("*.epub"))


def _make_epub_files_dict(dir_path: Path) -> Dict[str, List[Path]]:
    """Group files in *dir_path* by their suffix (extension)."""
    result: Dict[str, List[Path]] = defaultdict(list)
    for file_path in dir_path.rglob("*"):
        if file_path.is_file():
            result[file_path.suffix].append(file_path)
    return result


# ---------------------------------------------------------------------------
# EPUBHV – main processor
# ---------------------------------------------------------------------------


class EPUBHV:
    """Add Japanese ruby/furigana annotations to an EPUB file.

    Args:
        file_path: Path to the ``.epub`` file.
        use_llm: If ``True``, use DeepSeek API for context-aware kanji
            readings.  Requires ``DEEPSEEK_API_KEY`` env var.
        llm_model: DeepSeek model name (``deepseek-v4-flash`` or
            ``deepseek-v4-pro``).
        llm_api_key: DeepSeek API key (overrides env var).
        llm_base_url: API base URL.
    """

    _HTML_SUFFIXES = (".html", ".xhtml", ".htm")

    def __init__(
        self,
        file_path: Path,
        use_llm: bool = False,
        llm_model: str = "deepseek-v4-flash",
        llm_api_key: str | None = None,
        llm_base_url: str = "https://api.deepseek.com",
    ) -> None:
        if file_path.suffix.lower() != ".epub":
            raise ValueError(f"Not an .epub file: {file_path}")
        self._epub_file = file_path
        self._book_name = file_path.stem
        self._temp_dir: Path | None = None
        self._extract_dir: Path | None = None
        self._content_files: List[Path] = []

        # LLM settings
        self._use_llm = use_llm
        self._llm_model = llm_model
        self._llm_api_key = llm_api_key
        self._llm_base_url = llm_base_url

    @property
    def book_name(self) -> str:
        """The stem of the original EPUB filename."""
        return self._book_name

    # ---- extraction --------------------------------------------------------

    def _extract(self) -> None:
        """Extract the EPUB archive into a temporary directory."""
        self._temp_dir = Path(tempfile.mkdtemp(prefix="epubhv_"))
        self._extract_dir = self._temp_dir / self._book_name
        with zipfile.ZipFile(self._epub_file) as zf:
            zf.extractall(self._extract_dir)

    def _collect_content_files(self) -> None:
        """Build the list of HTML/XHTML content files to process."""
        assert self._extract_dir is not None
        files_dict = _make_epub_files_dict(self._extract_dir)
        self._content_files = []
        for suffix in self._HTML_SUFFIXES:
            self._content_files.extend(files_dict.get(suffix, []))

    def _apply_ruby(self) -> None:
        """Inject <ruby> annotations into every HTML content file.

        All files are processed in parallel with no artificial limit.
        Each file gets its own ``RubySoup``; the ``LLMRubyReader`` is shared.
        """
        llm_reader = None
        if self._use_llm:
            from .llm_ruby import LLMRubyReader

            llm_reader = LLMRubyReader(
                api_key=self._llm_api_key,
                model=self._llm_model,
                base_url=self._llm_base_url,
            )
            print(f"  [LLM] Using {self._llm_model} for context-aware readings")

        total = len(self._content_files)
        files = sorted(self._content_files, key=lambda f: f.name)

        def _process_one(idx: int, html_file: Path) -> tuple[int, str]:
            """Process a single file.  Returns (index, filename)."""
            print(f"  [{idx}/{total}] {html_file.name}")
            raw = html_file.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(raw, "html.parser",
                                 string_containers=string_containers)
            if soup.body is not None:
                ruby = RubySoup(is_ruby_rp=True, llm_reader=llm_reader)
                ruby.inject(soup.body)
            html_file.write_text(str(soup), encoding="utf-8")
            return (idx, html_file.name)

        workers = total if total else 1
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_process_one, i, f): i
                for i, f in enumerate(files, 1)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    print(f"  [ERROR] File failed: {exc}")

        if llm_reader is not None:
            stats = llm_reader.stats
            print(
                f"  [LLM] Done – {stats['api_calls']} API calls, "
                f"{stats['retries']} retries, "
                f"{stats['errors']} errors, "
                f"{stats['cache_hits']} cache hits"
            )
            if stats["errors"]:
                print(f"  [LLM] ⚠ {stats['errors']} batch(es) failed – "
                      f"fugashi readings used as fallback")

    # ---- packing -----------------------------------------------------------

    def _pack(self, dest: Path) -> Path:
        """Re-pack the annotated directory into a new EPUB."""
        assert self._extract_dir is not None
        assert self._temp_dir is not None
        output_name = f"{self._book_name}-ruby.epub"
        output_path = dest / output_name
        shutil.make_archive(
            base_name=str(output_path),
            format="zip",
            root_dir=self._extract_dir,
        )
        # shutil.make_archive appends .zip; rename to .epub
        zip_path = output_path.with_suffix(".epub.zip")
        zip_path.rename(output_path)
        return output_path

    def _cleanup(self) -> None:
        """Remove the temporary extraction directory."""
        if self._temp_dir is not None and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    # ---- public entry point ------------------------------------------------

    def run(self, dest: Path | None = None) -> Path:
        """Extract, annotate, re-pack and return the output EPUB path.

        Args:
            dest: Output directory. Defaults to the current working directory.

        Returns:
            Path to the generated ``-ruby.epub`` file.
        """
        dest = Path.cwd() if dest is None else dest
        try:
            self._extract()
            self._collect_content_files()
            self._apply_ruby()
            return self._pack(dest)
        finally:
            self._cleanup()
