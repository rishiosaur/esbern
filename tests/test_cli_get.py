from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from esbern.cli import main
from esbern.downloader import BookDownloadError, DownloadedBook


class GetCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.environment = patch.dict(
            os.environ, {"GOOGLE_BOOKS_API_KEY": "test-google-books-key"}
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    @patch("esbern.cli.download_book")
    def test_single_query_defaults_to_epub_then_pdf(self, download) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            book = Path(temp_dir) / "Book.epub"
            download.return_value = DownloadedBook("Book Name", book, "epub")
            result = self.runner.invoke(
                main,
                ["get", "Book", "Name", "--path", temp_dir, "--no-push"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            download.call_args.args[:2], ("Book Name", Path(temp_dir).resolve())
        )
        self.assertEqual(download.call_args.kwargs["formats"], ("epub", "pdf"))
        self.assertEqual(download.call_args.kwargs["source"], "libgen")
        self.assertEqual(
            download.call_args.kwargs["google_books_api_key"],
            "test-google-books-key",
        )
        self.assertTrue(download.call_args.kwargs["enrich_metadata"])
        self.assertIn("Downloaded Book.epub", result.output)

    @patch("esbern.cli.download_book")
    def test_bulk_continues_after_failure_and_exits_nonzero(self, download) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "books.txt"
            input_path.write_text("First Book\nSecond Book\n", encoding="utf-8")
            download.side_effect = [
                BookDownloadError("not found"),
                DownloadedBook("Second Book", root / "Second.epub", "epub"),
            ]
            result = self.runner.invoke(
                main,
                ["get", "-b", str(input_path), "--path", temp_dir, "--no-push"],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(download.call_count, 2)
        self.assertIn("1 downloaded, 1 failed", result.output)

    @patch("esbern.cli.download_book")
    def test_bulk_downloads_run_in_parallel(self, download) -> None:
        lock = threading.Lock()
        both_started = threading.Event()
        active = 0
        peak = 0

        def fake_download(query, destination, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_started.set()
            try:
                if not both_started.wait(timeout=2):
                    raise AssertionError("bulk downloads did not overlap")
                time.sleep(0.05)
                return DownloadedBook(query, destination / f"{query}.epub", "epub")
            finally:
                with lock:
                    active -= 1

        download.side_effect = fake_download
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "books.txt"
            input_path.write_text("First Book\nSecond Book\n", encoding="utf-8")
            result = self.runner.invoke(
                main,
                [
                    "get",
                    "-b",
                    str(input_path),
                    "--path",
                    temp_dir,
                    "--jobs",
                    "2",
                    "--no-push",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(peak, 2)
        self.assertIn("2 downloaded, 0 failed, 0 skipped", result.output)

    @patch("esbern.cli.download_book")
    def test_bulk_skips_existing_books_and_duplicate_queries(self, download) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "Le Guin, Ursula - The Left Hand of Darkness (1969).epub"
            existing.write_bytes(b"existing")
            input_path = root / "books.txt"
            input_path.write_text(
                "The Left Hand of Darkness Ursula Le Guin\n"
                "Second Book\n"
                " second   book \n",
                encoding="utf-8",
            )
            download.return_value = DownloadedBook(
                "Second Book", root / "Second Book.epub", "epub"
            )
            result = self.runner.invoke(
                main,
                ["get", "-b", str(input_path), "--path", temp_dir, "--no-push"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        download.assert_called_once()
        self.assertEqual(download.call_args.args[0], "Second Book")
        self.assertIn("1 downloaded, 0 failed, 2 skipped", result.output)

    def test_requires_query_or_bulk_file(self) -> None:
        result = self.runner.invoke(main, ["get"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Pass book/search terms", result.output)

    @patch("esbern.cli.download_book")
    def test_keyless_download_uses_local_metadata_fallback(self, download) -> None:
        download.return_value = DownloadedBook(
            "Book",
            Path("/tmp/Book.epub"),
            "epub",
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("esbern.book_metadata.PROJECT_ENV_PATH", Path("/missing/.env")),
        ):
            result = self.runner.invoke(
                main,
                ["get", "Book", "--no-push"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        download.assert_called_once()
        self.assertEqual(download.call_args.kwargs["google_books_api_key"], "")
        self.assertTrue(download.call_args.kwargs["enrich_metadata"])

    @patch("esbern.cli.download_book")
    def test_no_metadata_is_explicit_keyless_escape_hatch(self, download) -> None:
        download.return_value = DownloadedBook("Book", Path("/tmp/Book.epub"), "epub")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("esbern.book_metadata.PROJECT_ENV_PATH", Path("/missing/.env")),
        ):
            result = self.runner.invoke(
                main,
                ["get", "Book", "--no-metadata", "--no-push"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(download.call_args.kwargs["enrich_metadata"])

    @patch("esbern.cli._push_paths")
    @patch("esbern.cli.download_book")
    def test_single_download_pushes_only_the_new_book_by_default(
        self, download, push_paths
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            book = root / "Book.epub"
            book.write_bytes(b"book")
            download.return_value = DownloadedBook("Book", book, "epub")

            result = self.runner.invoke(
                main,
                ["get", "Book", "--path", temp_dir],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        push_paths.assert_called_once()
        self.assertEqual(push_paths.call_args.args, (root, [book]))
        self.assertEqual(push_paths.call_args.kwargs["workers"], 4)


if __name__ == "__main__":
    unittest.main()
