"""Library, cover, download, and sync operations used by the web server."""

from __future__ import annotations

import fcntl
import hashlib
import html
import io
import os
import re
import textwrap
import unicodedata
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader
from rich.console import Console

from esbern import config
from esbern.book_metadata import (
    BookMetadataError,
    google_books_api_key,
    read_epub_metadata,
)
from esbern.downloader import (
    SUPPORTED_FORMATS,
    SUPPORTED_SOURCES,
    DownloadedBook,
    download_book,
    find_existing_book,
)
from esbern.remarkable import connected
from esbern.sync import SUPPORTED_EXTS, SyncEvent
from esbern.sync import sync as run_sync

_CANONICAL_NAME = re.compile(
    r"^(?P<authors>.+) - (?P<title>.+) \((?P<year>1\d{3}|20\d{2})\)$"
)
_BOOK_ID = re.compile(r"^[0-9a-f]{24}$")
_MAX_COVER_BYTES = 16 * 1024 * 1024
_MAX_BULK_QUERIES = 500
_MAX_QUERY_LENGTH = 500
_IMAGE_TYPES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",
}


class ServerInputError(ValueError):
    """Raised when an HTTP-facing operation payload is invalid."""


@dataclass(frozen=True)
class CatalogBook:
    id: str
    relpath: str
    title: str
    authors: tuple[str, ...]
    year: str
    format: str
    folder: str
    size: int
    modified_at: str
    cover_version: str
    cover_url: str


@dataclass(frozen=True)
class CoverImage:
    content_type: str
    data: bytes
    version: str


def _book_id(relpath: str) -> str:
    return hashlib.sha256(relpath.encode("utf-8")).hexdigest()[:24]


def _iter_books(root: Path) -> Iterator[Path]:
    """Walk every ordinary PDF/EPUB below root without following symlinks."""
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            try:
                if entry.is_dir():
                    pending.append(entry)
                elif entry.is_file() and entry.suffix.casefold() in SUPPORTED_EXTS:
                    yield entry
            except OSError:
                continue


def _filename_metadata(path: Path) -> tuple[str, tuple[str, ...], str]:
    match = _CANONICAL_NAME.fullmatch(path.stem)
    if match is None:
        return path.stem, (), ""
    authors = tuple(
        author.strip() for author in match.group("authors").split(",") if author.strip()
    )
    return match.group("title"), authors, match.group("year")


def _catalog_book(root: Path, path: Path) -> CatalogBook:
    relpath = path.relative_to(root).as_posix()
    title, authors, year = _filename_metadata(path)
    if path.suffix.casefold() == ".epub":
        try:
            metadata = read_epub_metadata(path)
        except (BadZipFile, BookMetadataError, OSError, ValueError):
            metadata = None
        if metadata is not None:
            title = metadata.title or title
            authors = metadata.authors or authors
            year = metadata.year or year
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    cover_version = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
    book_id = _book_id(relpath)
    return CatalogBook(
        id=book_id,
        relpath=relpath,
        title=title,
        authors=authors,
        year=year,
        format=path.suffix[1:].casefold(),
        folder="" if path.parent == root else path.parent.relative_to(root).as_posix(),
        size=stat.st_size,
        modified_at=modified.isoformat(),
        cover_version=cover_version,
        cover_url=f"/api/books/{book_id}/cover?v={cover_version}",
    )


def catalog(root: Path) -> dict[str, object]:
    books: list[CatalogBook] = []
    for path in _iter_books(root):
        try:
            books.append(_catalog_book(root, path))
        except (OSError, ValueError):
            continue
    books.sort(key=lambda book: (book.title.casefold(), book.relpath.casefold()))
    return {
        "library": root.name,
        "count": len(books),
        "books": [asdict(book) for book in books],
    }


def _search_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value).casefold())
    return " ".join(
        "".join(char for char in text if not unicodedata.combining(char)).split()
    )


def search_catalog(root: Path, query: str, *, limit: int = 50) -> dict[str, object]:
    needle = _search_text(query)
    if not needle:
        raise ServerInputError("Search query must not be empty.")
    if not 1 <= limit <= 500:
        raise ServerInputError("Search limit must be between 1 and 500.")

    terms = needle.split()
    result = catalog(root)
    books = result["books"]
    assert isinstance(books, list)
    matches: list[tuple[int, dict[str, object]]] = []
    for book in books:
        title = _search_text(book["title"])
        authors = _search_text(" ".join(book["authors"]))
        searchable = _search_text(
            " ".join(
                (
                    str(book["title"]),
                    " ".join(book["authors"]),
                    str(book["year"]),
                    str(book["folder"]),
                    str(book["relpath"]),
                    str(book["format"]),
                )
            )
        )
        if not all(term in searchable for term in terms):
            continue
        score = sum(searchable.count(term) for term in terms)
        if needle == title:
            score += 1_000
        elif title.startswith(needle):
            score += 500
        elif needle in title:
            score += 250
        if needle in authors:
            score += 100
        matches.append((score, book))

    matches.sort(
        key=lambda item: (
            -item[0],
            str(item[1]["title"]).casefold(),
            str(item[1]["relpath"]).casefold(),
        )
    )
    selected = [book for _score, book in matches[:limit]]
    return {
        "library": result["library"],
        "query": query,
        "count": len(matches),
        "returned": len(selected),
        "books": selected,
    }


def _book_path(root: Path, book_id: str) -> tuple[Path, CatalogBook]:
    if not _BOOK_ID.fullmatch(book_id):
        raise ServerInputError("Invalid book id.")
    for path in _iter_books(root):
        try:
            relpath = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        if _book_id(relpath) == book_id:
            return path, _catalog_book(root, path)
    raise FileNotFoundError("Book not found.")


def _image_type(data: bytes) -> str | None:
    for signature, content_type in _IMAGE_TYPES.items():
        if not data.startswith(signature):
            continue
        if signature == b"RIFF" and data[8:12] != b"WEBP":
            continue
        return content_type
    return None


def _safe_image(data: bytes) -> tuple[str, bytes] | None:
    if not data or len(data) > _MAX_COVER_BYTES:
        return None
    content_type = _image_type(data)
    return (content_type, data) if content_type else None


def _epub_package_path(archive: ZipFile) -> str:
    from xml.etree import ElementTree as ET

    container = ET.fromstring(archive.read("META-INF/container.xml"))
    rootfile = container.find(
        ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
    )
    if rootfile is None:
        rootfile = container.find(".//{*}rootfile")
    package_path = rootfile.attrib.get("full-path", "") if rootfile is not None else ""
    if not package_path:
        raise ValueError("EPUB package path is missing")
    return package_path


def _extract_epub_cover(path: Path) -> tuple[str, bytes] | None:
    from xml.etree import ElementTree as ET

    try:
        with ZipFile(path) as archive:
            package_path = _epub_package_path(archive)
            package = ET.fromstring(archive.read(package_path))
            package_directory = Path(package_path).parent
            items: dict[str, tuple[str, str, str]] = {}
            cover_ids: list[str] = []
            for item in package.findall(".//{*}manifest/{*}item"):
                item_id = item.attrib.get("id", "")
                href = item.attrib.get("href", "")
                media_type = item.attrib.get("media-type", "")
                properties = item.attrib.get("properties", "")
                if item_id and href:
                    items[item_id] = (href, media_type, properties)
                    if "cover-image" in properties.split():
                        cover_ids.append(item_id)
            for meta in package.findall(".//{*}metadata/{*}meta"):
                if meta.attrib.get("name", "").casefold() == "cover":
                    cover_id = meta.attrib.get("content", "")
                    if cover_id:
                        cover_ids.append(cover_id)

            candidates: list[tuple[int, str]] = []
            for item_id, (href, media_type, _) in items.items():
                if not media_type.startswith("image/") or media_type == "image/svg+xml":
                    continue
                rank = 0
                if item_id in cover_ids:
                    rank += 100
                if "cover" in f"{item_id} {href}".casefold():
                    rank += 25
                candidates.append((rank, href))
            for _, href in sorted(candidates, key=lambda item: item[0], reverse=True):
                member = (package_directory / href).as_posix()
                try:
                    image = _safe_image(archive.read(member))
                except (KeyError, OSError):
                    continue
                if image:
                    return image
    except (BadZipFile, KeyError, OSError, ValueError, ET.ParseError):
        return None
    return None


def _extract_pdf_cover(path: Path) -> tuple[str, bytes] | None:
    try:
        reader = PdfReader(path)
        if not reader.pages:
            return None
        for image in reader.pages[0].images:
            candidate = _safe_image(image.data)
            if candidate:
                return candidate
    except Exception:  # noqa: BLE001 - malformed PDFs get a deterministic fallback
        return None
    return None


def _fallback_cover(book: CatalogBook) -> tuple[str, bytes]:
    palettes = (
        ("#d8c9b5", "#35302a", "#a35945"),
        ("#bfc9c2", "#26342f", "#5b766b"),
        ("#c9c4b8", "#30302b", "#8a6f49"),
        ("#c5ccd2", "#29333a", "#627b89"),
        ("#d2c2bf", "#392d2a", "#8b6259"),
    )
    palette_index = int(book.id[:4], 16) % len(palettes)
    background, foreground, accent = palettes[palette_index]
    title_lines = textwrap.wrap(book.title, width=21)[:7] or ["Untitled"]
    author = ", ".join(book.authors) or book.folder or book.format.upper()
    title_markup = "".join(
        f'<tspan x="82" dy="{0 if index == 0 else 82}">{html.escape(line)}</tspan>'
        for index, line in enumerate(title_lines)
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1200" viewBox="0 0 800 1200" role="img" aria-label="{html.escape(book.title)}">
<rect width="800" height="1200" fill="{background}"/>
<rect x="40" y="40" width="720" height="1120" rx="4" fill="none" stroke="{foreground}" stroke-opacity=".24" stroke-width="2"/>
<rect x="82" y="116" width="84" height="9" fill="{accent}"/>
<text x="82" y="248" fill="{foreground}" font-family="Arial, Helvetica, sans-serif" font-size="64" font-weight="650" letter-spacing="-2">{title_markup}</text>
<line x1="82" y1="1014" x2="718" y2="1014" stroke="{foreground}" stroke-opacity=".32"/>
<text x="82" y="1070" fill="{foreground}" fill-opacity=".8" font-family="Arial, Helvetica, sans-serif" font-size="24" letter-spacing="1">{html.escape(author[:54])}</text>
<text x="718" y="1110" text-anchor="end" fill="{foreground}" fill-opacity=".56" font-family="Arial, Helvetica, sans-serif" font-size="18" letter-spacing="3">ESBERN</text>
</svg>"""
    return "image/svg+xml", svg.encode("utf-8")


def cover(root: Path, book_id: str) -> CoverImage:
    path, book = _book_path(root, book_id)
    extracted = (
        _extract_epub_cover(path)
        if path.suffix.casefold() == ".epub"
        else _extract_pdf_cover(path)
    )
    content_type, data = extracted or _fallback_cover(book)
    return CoverImage(content_type, data, book.cover_version)


@contextmanager
def _library_lock(root: Path) -> Iterator[None]:
    state_directory = root / ".esbern"
    if state_directory.is_symlink():
        raise ServerInputError("The library .esbern directory must not be a symlink.")
    state_directory.mkdir(exist_ok=True)
    lock_path = state_directory / "server.lock"
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _sync_roots(root: Path) -> tuple[Path, ...]:
    """Return existing independent sync roots below a browsable library root."""
    if (root / ".esbern" / "state.json").is_file():
        return (root,)

    roots: list[Path] = []
    for directory, names, _files in os.walk(root, followlinks=False):
        current = Path(directory)
        names[:] = [
            name
            for name in names
            if not name.startswith(".") and not (current / name).is_symlink()
        ]
        if (current / ".esbern" / "state.json").is_file():
            roots.append(current)
            names.clear()
    return tuple(sorted(roots, key=lambda item: item.as_posix().casefold())) or (root,)


def _download_root(root: Path) -> Path:
    scopes = _sync_roots(root)
    configured = os.environ.get("ESBERN_INBOX_DIR", "").strip()
    if configured:
        destination = Path(configured).expanduser()
        if not destination.is_absolute():
            destination = root / destination
        destination = destination.resolve()
        if destination != root and root not in destination.parents:
            raise ServerInputError("ESBERN_INBOX_DIR must be inside the library root.")
        if not any(
            scope == destination or scope in destination.parents for scope in scopes
        ):
            raise ServerInputError(
                "ESBERN_INBOX_DIR must be inside an existing synchronized folder."
            )
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    if scopes == (root,):
        return root
    books = [scope for scope in scopes if scope.name.casefold() == "books"]
    if len(books) == 1:
        return books[0]
    if len(scopes) == 1:
        return scopes[0]
    raise ServerInputError(
        "This library contains multiple synchronized folders. Set "
        "ESBERN_INBOX_DIR to choose where downloaded books are installed."
    )


def _event_reporter(events: list[dict[str, object]]):
    def report(event: SyncEvent) -> None:
        if len(events) < 1_000:
            events.append(asdict(event))

    return report


def _sync(root: Path, *, workers: int) -> dict[str, object]:
    if not 1 <= workers <= 8:
        raise ServerInputError("Sync workers must be between 1 and 8.")
    cfg = config.load()
    libraries: list[dict[str, object]] = []
    totals: dict[str, int] = {}
    for sync_root in _sync_roots(root):
        events: list[dict[str, object]] = []
        with connected(cfg) as remarkable:
            stats = run_sync(
                remarkable,
                sync_root,
                restart=cfg.restart_xochitl,
                reporter=_event_reporter(events),
                workers=workers,
            )
        stats_payload = asdict(stats)
        for name, value in stats_payload.items():
            totals[name] = totals.get(name, 0) + value
        libraries.append(
            {
                "library": sync_root.relative_to(root).as_posix()
                if sync_root != root
                else ".",
                "stats": stats_payload,
                "events": events,
            }
        )
    return {
        "ok": True,
        "stats": totals,
        "events": libraries[0]["events"] if len(libraries) == 1 else [],
        "libraries": libraries,
    }


def synchronize(root: Path, *, workers: int = 4) -> dict[str, object]:
    with _library_lock(root):
        return _sync(root, workers=workers)


def _query_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        raise ServerInputError("Queries must be a list.")
    queries: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise ServerInputError("Every book query must be text.")
        query = " ".join(value.split())
        if not 3 <= len(query) <= _MAX_QUERY_LENGTH:
            raise ServerInputError(
                f"Every query must contain between 3 and {_MAX_QUERY_LENGTH} characters."
            )
        queries.append(query)
    if not queries:
        raise ServerInputError("At least one book query is required.")
    if len(queries) > _MAX_BULK_QUERIES:
        raise ServerInputError(
            f"A bulk request may contain at most {_MAX_BULK_QUERIES} queries."
        )
    return queries


def _download_result(result: DownloadedBook, root: Path) -> dict[str, object]:
    return {
        "query": result.query,
        "relpath": result.path.relative_to(root).as_posix(),
        "format": result.format,
        "source": result.source,
        "metadata": asdict(result.metadata) if result.metadata else None,
    }


def _find_existing_in_library(query: str, root: Path) -> Path | None:
    """Apply the CLI's filename match across every folder in the library."""
    directories = {root}
    directories.update(path.parent for path in _iter_books(root))
    for directory in sorted(directories, key=lambda path: path.as_posix().casefold()):
        existing = find_existing_book(query, directory)
        if existing is not None:
            return existing
    return None


def _download_many(
    root: Path,
    queries: Sequence[str],
    *,
    format_: str,
    source: str,
    metadata: bool,
    jobs: int,
    sync_workers: int,
) -> dict[str, object]:
    if format_ not in {"auto", *SUPPORTED_FORMATS}:
        raise ServerInputError("Format must be auto, epub, or pdf.")
    if source not in SUPPORTED_SOURCES:
        raise ServerInputError("Source must be auto, libgen, or arxiv.")
    if not 1 <= jobs <= 32:
        raise ServerInputError("Download jobs must be between 1 and 32.")
    formats = SUPPORTED_FORMATS if format_ == "auto" else (format_,)
    books_key = google_books_api_key()
    if metadata and source != "arxiv" and not books_key:
        raise ServerInputError(
            "Google Books metadata is enabled but GOOGLE_BOOKS_API_KEY is not configured."
        )

    pending: list[tuple[int, str]] = []
    skipped: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, query in enumerate(queries):
        key = " ".join(query.casefold().split())
        if key in seen:
            skipped.append({"query": query, "reason": "duplicate query"})
            continue
        seen.add(key)
        existing = _find_existing_in_library(query, root)
        if existing:
            skipped.append(
                {
                    "query": query,
                    "reason": "already exists",
                    "relpath": existing.relative_to(root).as_posix(),
                }
            )
            continue
        pending.append((index, query))

    destination = _download_root(root)
    downloaded_by_index: dict[int, dict[str, object]] = {}
    failed_by_index: dict[int, dict[str, str]] = {}

    def download_one(index: int, query: str) -> tuple[int, DownloadedBook]:
        console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
        result = download_book(
            query,
            destination,
            formats=formats,
            console=console,
            source=source,
            google_books_api_key=books_key,
            enrich_metadata=metadata,
        )
        return index, result

    if pending:
        with ThreadPoolExecutor(max_workers=min(jobs, len(pending))) as executor:
            futures = {
                executor.submit(download_one, index, query): (index, query)
                for index, query in pending
            }
            for future in as_completed(futures):
                index, query = futures[future]
                try:
                    completed_index, result = future.result()
                    downloaded_by_index[completed_index] = _download_result(
                        result, root
                    )
                except Exception as error:  # noqa: BLE001 - isolate each bulk item
                    failed_by_index[index] = {"query": query, "error": str(error)}

    downloaded = [downloaded_by_index[index] for index in sorted(downloaded_by_index)]
    failed = [failed_by_index[index] for index in sorted(failed_by_index)]
    sync_result: dict[str, object] | None = None
    if downloaded or skipped:
        try:
            sync_result = _sync(root, workers=sync_workers)
        except Exception as error:  # noqa: BLE001 - downloads remain installed locally
            sync_result = {
                "ok": False,
                "error": str(error),
                "error_type": type(error).__name__,
            }
    return {
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "sync": sync_result,
        "catalog": catalog(root),
    }


def install_books(root: Path, payload: dict[str, object]) -> dict[str, object]:
    queries = _query_list(payload.get("queries"))
    format_ = str(payload.get("format", "auto")).casefold()
    source = str(payload.get("source", "libgen")).casefold()
    metadata = payload.get("metadata", True)
    if not isinstance(metadata, bool):
        raise ServerInputError("Metadata must be true or false.")
    jobs = payload.get("jobs", 4)
    sync_workers = payload.get("sync_workers", 4)
    if isinstance(jobs, bool) or not isinstance(jobs, int):
        raise ServerInputError("Jobs must be an integer.")
    if isinstance(sync_workers, bool) or not isinstance(sync_workers, int):
        raise ServerInputError("Sync workers must be an integer.")
    with _library_lock(root):
        return _download_many(
            root,
            queries,
            format_=format_,
            source=source,
            metadata=metadata,
            jobs=jobs,
            sync_workers=sync_workers,
        )
