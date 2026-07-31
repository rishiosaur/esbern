from __future__ import annotations

import io
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from esbern.book_metadata import BookMetadata
from esbern.downloader import (
    BookDownloadError,
    _AttemptResult,
    _run_attempt,
    download_book,
    read_bulk_queries,
)


class BulkQueryTests(unittest.TestCase):
    def test_reads_trimmed_non_empty_lines_and_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "books.txt"
            path.write_text("\ufeff First Book \n\nSecond Book\r\n", encoding="utf-8")
            self.assertEqual(read_bulk_queries(path), ["First Book", "Second Book"])


class DownloadPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.console = Console(file=io.StringIO(), force_terminal=False,
                               color_system=None)

    @patch("esbern.downloader._run_attempt")
    def test_prefers_epub_without_trying_pdf(self, run_attempt) -> None:
        run_attempt.return_value.returncode = 0
        run_attempt.return_value.path = Path("/tmp/book.epub")
        result = download_book(
            "A Book", Path("/tmp"), console=self.console, executable="fake",
            enrich_metadata=False,
        )
        self.assertEqual(result.format, "epub")
        self.assertEqual(run_attempt.call_count, 1)

    @patch("esbern.downloader._run_attempt")
    def test_falls_back_to_pdf(self, run_attempt) -> None:
        failed = unittest.mock.Mock(returncode=1, path=None, error="no EPUB result")
        succeeded = unittest.mock.Mock(returncode=0, path=Path("/tmp/book.pdf"))
        run_attempt.side_effect = [failed, succeeded]
        result = download_book(
            "A Book", Path("/tmp"), console=self.console, executable="fake",
            enrich_metadata=False,
        )
        self.assertEqual(result.format, "pdf")
        self.assertEqual([call.args[2] for call in run_attempt.call_args_list],
                         ["epub", "pdf"])

    @patch("esbern.downloader.normalize_download")
    @patch("esbern.downloader._run_attempt")
    def test_enriches_and_renames_successful_libgen_download(
        self, run_attempt, normalize
    ) -> None:
        raw = Path("/tmp/raw.epub")
        canonical = Path("/tmp/Author - Title (2024).epub")
        metadata = BookMetadata(
            google_id="id", title="Title", authors=("Author",),
            published_date="2024",
        )
        run_attempt.return_value = _AttemptResult(0, raw, ())
        normalize.return_value = (canonical, metadata)

        result = download_book(
            "Title Author", Path("/tmp"), executable="fake",
            google_books_api_key="secret",
        )

        normalize.assert_called_once_with(raw, "Title Author", api_key="secret")
        self.assertEqual(result.path, canonical)
        self.assertEqual(result.metadata, metadata)

    @patch("esbern.downloader.normalize_download")
    @patch("esbern.downloader._run_attempt")
    def test_reports_local_fallback_when_google_is_unavailable(
        self, run_attempt, normalize
    ) -> None:
        raw = Path("/tmp/raw.epub")
        canonical = Path("/tmp/Author - Title (2024).epub")
        metadata = BookMetadata(
            google_id="",
            title="Title",
            authors=("Author",),
            published_date="2024",
        )
        run_attempt.return_value = _AttemptResult(0, raw, ())
        normalize.return_value = (canonical, metadata)
        progress = []

        result = download_book(
            "Title Author",
            Path("/tmp"),
            executable="fake",
            google_books_api_key="exhausted",
            progress_callback=lambda status, detail: progress.append((status, detail)),
        )

        self.assertEqual(result.path, canonical)
        self.assertIn(
            (
                "Google Books unavailable; LibGen metadata cleaned",
                canonical.name,
            ),
            progress,
        )

    def test_rejects_short_search_terms(self) -> None:
        with self.assertRaisesRegex(BookDownloadError, "at least 3"):
            download_book("it", Path("/tmp"), console=self.console,
                          executable="fake")

    @patch("esbern.downloader._download_from_arxiv")
    @patch("esbern.downloader.shutil.which")
    def test_arxiv_identifier_uses_arxiv_before_libgen(self, which, arxiv) -> None:
        arxiv.return_value = Path("/tmp/Attention.pdf")

        result = download_book("arXiv:1706.03762", Path("/tmp"), source="auto")

        self.assertEqual(result.source, "arxiv")
        which.assert_not_called()

    @patch("esbern.downloader._download_from_arxiv")
    @patch("esbern.downloader._run_attempt")
    def test_auto_falls_back_from_libgen_to_arxiv(self, run_attempt, arxiv) -> None:
        run_attempt.return_value = _AttemptResult(1, None, ("Error: unavailable",))
        arxiv.return_value = Path("/tmp/Paper.pdf")

        result = download_book(
            "A Research Paper", Path("/tmp"), executable="fake", source="auto"
        )

        self.assertEqual(run_attempt.call_count, 2)
        self.assertEqual(result.source, "arxiv")

    @patch("esbern.downloader._download_from_arxiv")
    @patch("esbern.downloader._run_attempt")
    def test_forced_libgen_never_calls_arxiv(self, run_attempt, arxiv) -> None:
        run_attempt.return_value = _AttemptResult(
            0, Path("/tmp/Nonfiction.epub"), ()
        )

        result = download_book(
            "A Nonfiction Book", Path("/tmp"), executable="fake", source="libgen",
            enrich_metadata=False,
        )

        self.assertEqual(result.source, "libgen")
        arxiv.assert_not_called()

    @patch("esbern.downloader._download_from_arxiv")
    @patch("esbern.downloader.shutil.which")
    def test_forced_arxiv_search_terms_never_call_libgen(self, which, arxiv) -> None:
        arxiv.return_value = Path("/tmp/Matching Paper.pdf")

        result = download_book(
            "Matching Paper Author Name", Path("/tmp"), source="arxiv"
        )

        self.assertEqual(result.source, "arxiv")
        which.assert_not_called()


class SubprocessProgressTests(unittest.TestCase):
    @staticmethod
    def _fake_downloader(root: Path, *, fail: bool = False) -> Path:
        executable = root / "fake-downloader"
        exit_line = "sys.exit(1)" if fail else "print(f\"Downloaded to {target}\", flush=True)"
        executable.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import pathlib
            import sys
            import time

            output = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
            book_format = sys.argv[sys.argv.index("--format") + 1]
            print("Searching test mirror...", flush=True)
            print(f"Downloading {{book_format.upper()}}...", flush=True)
            target = output / f"Example.{{book_format}}"
            with target.open("wb") as stream:
                stream.write(b"1234")
                stream.flush()
                time.sleep(0.2)
                stream.write(b"5678")
            {exit_line}
        """), encoding="utf-8")
        os.chmod(executable, 0o755)
        return executable

    def test_runs_external_downloader_and_reports_file_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = self._fake_downloader(root)
            destination = root / "books"
            output = io.StringIO()

            result = _run_attempt(
                str(executable), "Example", "epub", destination,
                Console(file=output, force_terminal=False, color_system=None),
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.path, (destination / "Example.epub").resolve())
            self.assertEqual(result.path.read_bytes(), b"12345678")
            self.assertIn("Example.epub", output.getvalue())
            self.assertIn("8 B", output.getvalue())

    def test_failed_download_does_not_leave_a_partial_book(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = self._fake_downloader(root, fail=True)
            destination = root / "books"

            result = _run_attempt(
                str(executable), "Example", "epub", destination,
                Console(file=io.StringIO(), force_terminal=False, color_system=None),
            )

            self.assertEqual(result.returncode, 1)
            self.assertIsNone(result.path)
            self.assertEqual(list(destination.glob("*.epub")), [])


if __name__ == "__main__":
    unittest.main()
