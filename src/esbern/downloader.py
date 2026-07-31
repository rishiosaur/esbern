"""Book and paper downloads with automatic source fallback.

LibGen book retrieval is delegated to the external ``libgen-downloader``
command. arXiv retrieval uses its public API and direct PDF links.
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from esbern.arxiv import (
    ArxivDownloadError,
    fetch_arxiv,
    looks_like_paper_identifier,
)
from esbern.book_metadata import (
    BookMetadata,
    BookMetadataError,
    normalize_download,
)

SUPPORTED_FORMATS = ("epub", "pdf")
SUPPORTED_SOURCES = ("auto", "libgen", "arxiv")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DESTINATION_LOCK = threading.Lock()
ProgressCallback = Callable[[str | None, str | None], None]


@dataclass(frozen=True)
class DownloadedBook:
    query: str
    path: Path
    format: str
    source: str = "libgen"
    metadata: BookMetadata | None = None


@dataclass(frozen=True)
class _AttemptResult:
    returncode: int
    path: Path | None
    output: tuple[str, ...]

    @property
    def error(self) -> str:
        for line in reversed(self.output):
            if line.lower().startswith("error:"):
                return line.split(":", 1)[1].strip()
        return (
            self.output[-1]
            if self.output
            else "the downloader exited without an error message"
        )


class BookDownloadError(RuntimeError):
    """Raised after every requested format failed for one query."""


def read_bulk_queries(path: Path) -> list[str]:
    """Read non-empty UTF-8 lines, accepting a BOM from text editors."""
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def find_existing_book(query: str, directory: Path) -> Path | None:
    """Find a PDF/EPUB whose filename contains every query term.

    Comparing case-folded terms instead of the raw string also recognizes
    filenames that reorder author and title or include punctuation and year
    metadata.
    """
    query_terms = set(re.findall(r"[^\W_]+", query.casefold()))
    if not query_terms:
        return None
    try:
        entries = directory.iterdir()
    except FileNotFoundError:
        return None
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in {".epub", ".pdf"}:
            continue
        filename_terms = set(re.findall(r"[^\W_]+", entry.stem.casefold()))
        if query_terms <= filename_terms:
            return entry.resolve()
    return None


def _human_bytes(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def _snapshot(directory: Path, extension: str) -> dict[Path, tuple[int, int]]:
    snapshot: dict[Path, tuple[int, int]] = {}
    try:
        entries = directory.iterdir()
    except FileNotFoundError:
        return snapshot
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() != f".{extension}":
            continue
        try:
            stat = entry.stat()
        except FileNotFoundError:
            continue
        snapshot[entry.resolve()] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _changed_file(
    directory: Path, extension: str, before: dict[Path, tuple[int, int]]
) -> Path | None:
    changed: list[tuple[int, Path]] = []
    for path, state in _snapshot(directory, extension).items():
        if before.get(path) != state:
            changed.append((state[1], path))
    return max(changed, default=(0, None), key=lambda item: item[0])[1]


def _path_from_output(lines: Sequence[str], output_directory: Path) -> Path | None:
    prefix = "Downloaded to "
    for line in reversed(lines):
        if not line.startswith(prefix):
            continue
        path = Path(line[len(prefix) :].strip()).expanduser()
        if not path.is_absolute():
            path = output_directory / path
        return path.resolve()
    return None


def _drain_lines(
    lines: queue.SimpleQueue[str],
    collected: list[str],
    progress: Progress,
    task_id: int,
    progress_callback: ProgressCallback | None = None,
) -> None:
    while True:
        try:
            line = lines.get_nowait()
        except queue.Empty:
            return
        cleaned = _ANSI_ESCAPE.sub("", line).strip()
        if not cleaned:
            continue
        collected.append(cleaned)
        if not cleaned.startswith("Downloaded to "):
            progress.update(task_id, description=cleaned)
            if progress_callback:
                progress_callback(cleaned, None)


def _run_attempt_in_directory(
    executable: str,
    query: str,
    book_format: str,
    working_directory: Path,
    console: Console,
    progress_callback: ProgressCallback | None = None,
) -> _AttemptResult:
    before = _snapshot(working_directory, book_format)
    command = [
        executable,
        "get",
        query,
        "--format",
        book_format,
        "--output",
        str(working_directory),
    ]

    try:
        process = subprocess.Popen(
            command,
            cwd=working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as error:
        return _AttemptResult(127, None, (f"Error: {error}",))

    assert process.stdout is not None
    line_queue: queue.SimpleQueue[str] = queue.SimpleQueue()

    def read_output() -> None:
        for line in process.stdout:
            line_queue.put(line)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    collected: list[str] = []
    changed_path: Path | None = None

    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", markup=False),
        TextColumn("{task.fields[detail]}", markup=False),
        TimeElapsedColumn(),
        console=console,
        transient=False,
        refresh_per_second=8,
    )
    with progress:
        initial_status = f"Starting LibGen {book_format.upper()} search…"
        task_id = progress.add_task(initial_status, total=None, detail="")
        if progress_callback:
            progress_callback(initial_status, None)
        try:
            while process.poll() is None:
                _drain_lines(
                    line_queue, collected, progress, task_id, progress_callback
                )
                candidate = _changed_file(working_directory, book_format, before)
                if candidate is not None:
                    changed_path = candidate
                    try:
                        size = candidate.stat().st_size
                    except FileNotFoundError:
                        size = 0
                    progress.update(
                        task_id,
                        detail=f"{candidate.name} · {_human_bytes(size)}",
                    )
                    if progress_callback:
                        progress_callback(
                            None, f"{candidate.name} · {_human_bytes(size)}"
                        )
                time.sleep(0.1)
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            raise

        reader.join(timeout=1)
        _drain_lines(line_queue, collected, progress, task_id, progress_callback)
        candidate = _changed_file(working_directory, book_format, before)
        if candidate is not None:
            changed_path = candidate
            try:
                size = candidate.stat().st_size
            except FileNotFoundError:
                size = 0
            progress.update(task_id, detail=f"{candidate.name} · {_human_bytes(size)}")
        final_status = (
            f"LibGen {book_format.upper()} complete"
            if process.returncode == 0
            else f"LibGen {book_format.upper()} unavailable"
        )
        progress.update(task_id, description=final_status)
        if progress_callback:
            progress_callback(final_status, None)

    process.stdout.close()

    reported_path = _path_from_output(collected, working_directory)
    downloaded_path = (
        reported_path if reported_path and reported_path.exists() else changed_path
    )
    if process.returncode == 0 and downloaded_path is None:
        collected.append(
            "Error: the downloader reported success but no downloaded file was found"
        )
        return _AttemptResult(1, None, tuple(collected))
    return _AttemptResult(process.returncode or 0, downloaded_path, tuple(collected))


def _unique_destination(path: Path) -> Path:
    """Avoid overwriting an existing download with the same filename."""
    if not path.exists():
        return path
    for number in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise BookDownloadError(f"Could not find an unused filename for {path.name}.")


def _run_attempt(
    executable: str,
    query: str,
    book_format: str,
    output_directory: Path,
    console: Console,
    progress_callback: ProgressCallback | None = None,
) -> _AttemptResult:
    """Download through a staging directory so partial files are never synced."""
    output_directory.mkdir(parents=True, exist_ok=True)
    staging_parent = output_directory / ".esbern"
    staging_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="get-", dir=staging_parent) as temp_dir:
        attempt = _run_attempt_in_directory(
            executable,
            query,
            book_format,
            Path(temp_dir),
            console,
            progress_callback,
        )
        if attempt.returncode != 0 or attempt.path is None:
            return _AttemptResult(attempt.returncode, None, attempt.output)
        try:
            # Bulk workers can finish simultaneously, including with the same
            # filename. Choose and claim the destination atomically.
            with _DESTINATION_LOCK:
                destination = _unique_destination(output_directory / attempt.path.name)
                attempt.path.replace(destination)
        except OSError as error:
            return _AttemptResult(
                1,
                None,
                attempt.output + (f"Error: could not save the download: {error}",),
            )
        return _AttemptResult(0, destination.resolve(), attempt.output)


def _download_from_arxiv(
    query: str,
    output_directory: Path,
    console: Console,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    staging_parent = output_directory / ".esbern"
    staging_parent.mkdir(exist_ok=True)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", markup=False),
        TextColumn("{task.fields[detail]}", markup=False),
        TimeElapsedColumn(),
        console=console,
        transient=False,
        refresh_per_second=8,
    )
    with tempfile.TemporaryDirectory(
        prefix="get-arxiv-", dir=staging_parent
    ) as temp_dir:
        with progress:
            task_id = progress.add_task("Searching arXiv…", total=None, detail="")

            def report(status: str | None, detail: str | None) -> None:
                changes = {}
                if status is not None:
                    changes["description"] = status
                if detail is not None:
                    changes["detail"] = detail
                progress.update(task_id, **changes)
                if progress_callback:
                    progress_callback(status, detail)

            result = fetch_arxiv(query, Path(temp_dir), report)
            report("arXiv download complete", None)
        try:
            with _DESTINATION_LOCK:
                destination = _unique_destination(output_directory / result.path.name)
                result.path.replace(destination)
        except OSError as error:
            raise ArxivDownloadError(
                f"could not save the arXiv download: {error}"
            ) from error
    return destination.resolve()


def download_book(
    query: str,
    output_directory: Path,
    *,
    formats: Sequence[str] = SUPPORTED_FORMATS,
    console: Console | None = None,
    executable: str | None = None,
    source: str = "libgen",
    progress_callback: ProgressCallback | None = None,
    google_books_api_key: str | None = None,
    enrich_metadata: bool = True,
) -> DownloadedBook:
    """Download one query using automatic LibGen/arXiv fallback."""
    query = query.strip()
    if len(query) < 3:
        raise BookDownloadError("Search terms must be at least 3 characters long.")
    if not formats or any(
        book_format not in SUPPORTED_FORMATS for book_format in formats
    ):
        raise ValueError("formats must contain epub and/or pdf")
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SUPPORTED_SOURCES)}")

    destination = output_directory.expanduser().resolve()
    progress_console = console or Console(stderr=True)
    errors: list[str] = []
    tried_arxiv = False

    def try_arxiv() -> DownloadedBook | None:
        nonlocal tried_arxiv
        tried_arxiv = True
        if "pdf" not in formats:
            errors.append("arXiv: only PDF downloads are available")
            return None
        try:
            path = _download_from_arxiv(
                query, destination, progress_console, progress_callback
            )
            return DownloadedBook(query=query, path=path, format="pdf", source="arxiv")
        except ArxivDownloadError as error:
            errors.append(f"arXiv: {error}")
            return None

    if source == "arxiv" or (source == "auto" and looks_like_paper_identifier(query)):
        result = try_arxiv()
        if result:
            return result
        if source == "arxiv":
            raise BookDownloadError(f'Could not download "{query}". {errors[-1]}')

    if source in {"auto", "libgen"}:
        resolved_executable = executable or shutil.which("libgen-downloader")
        if not resolved_executable:
            errors.append("LibGen: libgen-downloader was not found on PATH")
        else:
            for index, book_format in enumerate(formats):
                attempt = _run_attempt(
                    resolved_executable,
                    query,
                    book_format,
                    destination,
                    progress_console,
                    progress_callback,
                )
                if attempt.returncode == 0 and attempt.path is not None:
                    path = attempt.path
                    metadata = None
                    if enrich_metadata:
                        if progress_callback:
                            progress_callback("Resolving book metadata…", path.name)
                        try:
                            path, metadata = normalize_download(
                                path,
                                query,
                                api_key=google_books_api_key,
                            )
                        except BookMetadataError as error:
                            raise BookDownloadError(
                                f'Downloaded "{query}" to {path}, but could not '
                                f"normalize its metadata: {error}"
                            ) from error
                        if progress_callback:
                            if metadata.google_id:
                                progress_callback(
                                    "Google Books metadata saved", path.name
                                )
                            else:
                                progress_callback(
                                    "Google Books unavailable; LibGen metadata cleaned",
                                    path.name,
                                )
                    return DownloadedBook(
                        query=query,
                        path=path,
                        format=book_format,
                        source="libgen",
                        metadata=metadata,
                    )
                errors.append(f"LibGen {book_format.upper()}: {attempt.error}")
                if index + 1 < len(formats):
                    progress_console.print(
                        f"No usable LibGen {book_format.upper()} download; trying "
                        f"{formats[index + 1].upper()}…",
                        style="yellow",
                    )

    if source == "auto" and not tried_arxiv:
        if errors:
            progress_console.print(
                "No usable LibGen download; trying arXiv…", style="yellow"
            )
        result = try_arxiv()
        if result:
            return result

    details = "; ".join(errors)
    raise BookDownloadError(f'Could not download "{query}". {details}')
