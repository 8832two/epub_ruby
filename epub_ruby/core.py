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
from typing import TYPE_CHECKING, Dict, List, Set, Callable, Optional

from bs4 import BeautifulSoup

from .ruby import RubySoup, string_containers
from .exceptions import EPUBInvalidError, EPUBExtractionError, EPUBPackingError
from .logger import get_logger

if TYPE_CHECKING:
    from .llm_ruby import LLMRubyReader

_logger = get_logger(__name__)


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
        use_llm: If ``True``, use LLM API for context-aware kanji readings.
        llm_model: Model name (e.g. ``deepseek-v4-flash``).
        llm_api_key: API key (overrides env var).
        llm_base_url: API base URL.
        llm_batch_size: Max sentences per API call (default 60).
        llm_pool: An :class:`APIPool` for multi-provider support.
    """

    _HTML_SUFFIXES = (".html", ".xhtml", ".htm")

    def __init__(
        self,
        file_path: Path,
        use_llm: bool = False,
        llm_model: str = "deepseek-v4-flash",
        llm_api_key: Optional[str] = None,
        llm_base_url: str = "https://api.deepseek.com",
        llm_batch_size: int = 60,
        llm_pool=None,
        llm_max_concurrent: int = 0,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        # Validate input file
        file_path = Path(file_path)
        if not file_path.exists():
            _logger.error(f"EPUB file not found: {file_path}")
            raise EPUBInvalidError(f"File not found: {file_path}")
        if file_path.suffix.lower() != ".epub":
            _logger.error(f"Invalid file extension: {file_path.suffix}")
            raise EPUBInvalidError(
                f"Not an EPUB file (expected .epub, got {file_path.suffix})"
            )
        if not file_path.is_file():
            _logger.error(f"Path is not a file: {file_path}")
            raise EPUBInvalidError(f"Path is not a regular file: {file_path}")

        # Validate batch size
        if llm_batch_size < 1:
            _logger.warning(
                f"Invalid batch_size={llm_batch_size}, using default (60)"
            )
            llm_batch_size = 60

        self._epub_file = file_path
        self._book_name = file_path.stem
        self._temp_dir: Optional[Path] = None
        self._extract_dir: Optional[Path] = None
        self._content_files: List[Path] = []

        # LLM settings
        self._use_llm = use_llm
        self._llm_model = llm_model
        self._llm_api_key = llm_api_key
        self._llm_base_url = llm_base_url
        self._llm_batch_size = llm_batch_size
        self._llm_pool = llm_pool
        self._llm_max_concurrent = max(0, llm_max_concurrent)

        # Progress callback: fn(current, total) called per content file
        self._progress_callback = progress_callback

        _logger.info(
            f"Initialized EPUBHV for: {self._book_name}.epub "
            f"(LLM: {use_llm})"
        )

    @property
    def book_name(self) -> str:
        """The stem of the original EPUB filename."""
        return self._book_name

    # ---- extraction --------------------------------------------------------

    def _extract(self) -> None:
        """Extract the EPUB archive into a temporary directory."""
        try:
            _logger.info(f"Extracting EPUB: {self._epub_file}")
            self._temp_dir = Path(tempfile.mkdtemp(prefix="epubhv_"))
            self._extract_dir = self._temp_dir / self._book_name
            with zipfile.ZipFile(self._epub_file) as zf:
                zf.extractall(self._extract_dir)
            _logger.info(f"Extraction complete: {self._extract_dir}")
        except zipfile.BadZipFile as e:
            _logger.error(f"Corrupted EPUB file: {self._epub_file}")
            raise EPUBExtractionError(
                f"Failed to extract EPUB (corrupted file): {e}"
            ) from e
        except Exception as e:
            _logger.error(f"Extraction error: {e}", exc_info=True)
            raise EPUBExtractionError(f"Failed to extract EPUB: {e}") from e

    def _collect_content_files(self) -> None:
        """Build the list of HTML/XHTML content files to process."""
        assert self._extract_dir is not None
        files_dict = _make_epub_files_dict(self._extract_dir)
        self._content_files = []
        for suffix in self._HTML_SUFFIXES:
            self._content_files.extend(files_dict.get(suffix, []))

    def _apply_ruby(self) -> None:
        """Inject <ruby> annotations into every HTML content file.

        Two-phase approach for LLM mode:
        1. Collect ALL sentences from all files (parallel, no API calls)
        2. ONE batch API call (internally parallel via ThreadPoolExecutor)
        3. Apply readings to all files (parallel, no API calls)

        This eliminates the N×M thread explosion that occurred when
        each file independently dispatched its own ThreadPoolExecutor.
        """
        llm_reader = None
        if self._use_llm:
            from .llm_ruby import LLMRubyReader

            if self._llm_pool is not None:
                llm_reader = LLMRubyReader(
                    pool=self._llm_pool,
                    batch_size=self._llm_batch_size,
                    max_concurrent=self._llm_max_concurrent,
                    progress_callback=self._progress_callback,
                )
                print(f"  [LLM] Using API pool ({llm_reader._pool.provider_count} provider(s))")
            else:
                llm_reader = LLMRubyReader(
                    api_key=self._llm_api_key,
                    model=self._llm_model,
                    base_url=self._llm_base_url,
                    batch_size=self._llm_batch_size,
                    max_concurrent=self._llm_max_concurrent,
                    progress_callback=self._progress_callback,
                )
                print(f"  [LLM] Using {self._llm_model} for context-aware readings")

        total = len(self._content_files)
        files = sorted(self._content_files, key=lambda f: f.name)

        if self._use_llm and llm_reader is not None:
            # ── LLM mode: two-phase (collect → batch API → apply) ──

            # Phase 1: collect all (sentence, words) from all files
            all_items: list[tuple[str, list[str]]] = []
            ruby_soups: list[RubySoup] = []
            soups: list[BeautifulSoup] = []

            def _collect_one(html_file: Path) -> tuple[RubySoup, BeautifulSoup]:
                raw = html_file.read_text(encoding="utf-8", errors="ignore")
                soup = BeautifulSoup(raw, "html.parser",
                                     string_containers=string_containers)
                ruby = RubySoup(is_ruby_rp=True, llm_reader=llm_reader)
                if soup.body is not None:
                    ruby._collect_segments(soup.body, all_items)
                return ruby, soup

            print(f"  [Phase 1] Collecting sentences from {total} file(s)...")
            with ThreadPoolExecutor(max_workers=total if total else 1) as executor:
                futures = {executor.submit(_collect_one, f): f for f in files}
                for future in as_completed(futures):
                    try:
                        ruby, soup = future.result()
                        ruby_soups.append(ruby)
                        soups.append(soup)
                    except Exception as exc:
                        print(f"  [ERROR] Collection failed: {exc}")

            # Phase 2: one batch API call (internally fully parallel)
            if all_items:
                # Compute expected chunk count for combined progress
                bs = self._llm_batch_size
                total_chunks = max(1, (len(all_items) + bs - 1) // bs)
                total_work = total_chunks + total  # chunks + files

                # Wrap progress callback so Phase 2 + Phase 3 share one scale
                if self._progress_callback:
                    _orig_cb = self._progress_callback
                    def _phase2_cb(done_chunks: int, _total_chunks: int) -> None:
                        _orig_cb(done_chunks, total_work)
                    llm_reader._progress_callback = _phase2_cb

                print(f"  [Phase 2] {len(all_items)} sentences → LLM batch API...")
                batch_readings = llm_reader.get_readings_batch(all_items)
            else:
                batch_readings = {}
                total_chunks = 0
                total_work = total

            # Phase 3: apply readings + write back
            print(f"  [Phase 3] Applying readings to {len(ruby_soups)} file(s)...")

            def _apply_one(idx: int, ruby: RubySoup, soup: BeautifulSoup,
                          html_file: Path) -> tuple[int, str]:
                if soup.body is not None:
                    ruby._batch_readings = batch_readings
                    ruby._apply(soup.body)
                html_file.write_text(str(soup), encoding="utf-8")
                print(f"  [{idx}/{total}] {html_file.name}")
                return (idx, html_file.name)

            total_files = len(files)
            completed = 0
            with ThreadPoolExecutor(max_workers=len(files) if len(files) else 1) as executor:
                futures2 = {
                    executor.submit(_apply_one, i, rs, sp, f): i
                    for i, (rs, sp, f) in enumerate(
                        zip(ruby_soups, soups, files), 1
                    )
                }
                for future in as_completed(futures2):
                    try:
                        future.result()
                        completed += 1
                        if self._progress_callback:
                            self._progress_callback(total_chunks + completed, total_work)
                    except Exception as exc:
                        print(f"  [ERROR] Apply failed: {exc}")
                        completed += 1
                        if self._progress_callback:
                            self._progress_callback(total_chunks + completed, total_work)

        else:
            # ── Dictionary mode: simple parallel ──
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
            completed = 0
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_process_one, i, f): i
                    for i, f in enumerate(files, 1)
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                        completed += 1
                        if self._progress_callback:
                            self._progress_callback(completed, total)
                    except Exception as exc:
                        print(f"  [ERROR] File failed: {exc}")
                        completed += 1
                        if self._progress_callback:
                            self._progress_callback(completed, total)

        if llm_reader is not None:
            stats = llm_reader.stats
            print(
                f"  [LLM] Done – {stats['api_calls']} API calls, "
                f"{stats['retries']} retries, "
                f"{stats['total_annotated']} words annotated, "
                f"{stats['cache_hits']} cache hits"
            )
            if stats["errors"]:
                print(f"  [LLM] ⚠ {stats['errors']} batch(es) failed, "
                      f"{stats['total_failed']} sentences skipped – "
                      f"check error log")
            if stats["total_annotated"] == 0 and stats["api_calls"] > 0:
                print(f"  [LLM] ⚠ WARNING: API called but ZERO words annotated! "
                      f"LLM may be returning empty results.")

    # ---- packing -----------------------------------------------------------

    def _pack(self, dest: Path) -> Path:
        """Re-pack the annotated directory into a new EPUB."""
        if self._extract_dir is None or self._temp_dir is None:
            raise EPUBPackingError("EPUB not extracted (extract_dir is None)")
        try:
            output_name = f"{self._book_name}-ruby.epub"
            output_path = dest / output_name
            _logger.info(f"Packing EPUB to: {output_path}")
            shutil.make_archive(
                base_name=str(output_path),
                format="zip",
                root_dir=self._extract_dir,
            )
            # shutil.make_archive appends .zip; rename to .epub
            zip_path = output_path.with_suffix(".epub.zip")
            zip_path.rename(output_path)
            _logger.info(f"Packing complete: {output_path}")
            return output_path
        except Exception as e:
            _logger.error(f"Packing error: {e}", exc_info=True)
            raise EPUBPackingError(f"Failed to pack EPUB: {e}") from e

    def _cleanup(self) -> None:
        """Remove the temporary extraction directory."""
        if self._temp_dir is not None and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    # ---- public entry point ------------------------------------------------

    def run(self, dest: Optional[Path] = None) -> Path:
        """Extract, annotate, re-pack and return the output EPUB path.

        Args:
            dest: Output directory. Defaults to the current working directory.

        Returns:
            Path to the generated ``-ruby.epub`` file.

        Raises:
            EPUBInvalidError: If input file is invalid.
            EPUBExtractionError: If extraction fails.
            EPUBPackingError: If re-packing fails.
        """
        dest = Path.cwd() if dest is None else dest
        dest.mkdir(parents=True, exist_ok=True)
        _logger.info(f"Starting EPUB processing: {self._epub_file}")
        try:
            self._extract()
            self._collect_content_files()
            _logger.info(
                f"Found {len(self._content_files)} content file(s) to process"
            )
            self._apply_ruby()
            output_path = self._pack(dest)
            _logger.info(f"Successfully processed: {output_path}")
            return output_path
        except Exception as e:
            _logger.error(f"Processing failed: {e}", exc_info=True)
            raise
        finally:
            self._cleanup()
