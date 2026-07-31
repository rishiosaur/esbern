"""Google Books metadata lookup and canonical downloaded-book filenames."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import requests

GOOGLE_BOOKS_API_URL = "https://www.googleapis.com/books/v1/volumes"
GOOGLE_BOOKS_USER_AGENT = "esbern/0.1 (personal book metadata organizer)"
PROJECT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

_DC = "http://purl.org/dc/elements/1.1/"
_OPF = "http://www.idpf.org/2007/opf"
_DCTERMS = "http://purl.org/dc/terms/"
_CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
_YEAR = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)")
_ISBN = re.compile(r"(?<!\d)(?:97[89][\d\s-]{10,16}|[\dXx][\dXx\s-]{8,14})(?!\d)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FILENAME_SEPARATORS = str.maketrans({'"': "'", "/": "-", "\\": "-", "|": "-"})
_FILENAME_DROP = re.compile(r"[<>?*]")
_MARKETING_SUFFIX = re.compile(
    r"\s*\((?:national book award finalist|movie tie[- ]in|"
    r"motion picture tie[- ]in|media tie[- ]in|the sunday times bestseller|"
    r"sunday times bestseller|read with jenna pick|mti)\)\s*$",
    re.IGNORECASE,
)
_MARKETING_WORDS = re.compile(
    r"\b(?:award|bestseller|shortlist|tie[- ]in)\b", re.IGNORECASE
)
_GENERIC_SUBTITLE = re.compile(
    r"^(?:an? (?:novel|story)|stories|\d+(?:st|nd|rd|th) anniversary edition|"
    r"(?:a )?read with .+|the sequel to .+|winner of .+)$",
    re.IGNORECASE,
)
_TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
_API_LOCK = threading.Lock()
_CANONICAL_RENAME_LOCK = threading.Lock()
_GOOGLE_BACKOFF_UNTIL = 0.0
_GOOGLE_BACKOFF_REASON = ""
_GOOGLE_BACKOFF_SECONDS = 15 * 60
_LIBGEN_PUBLISHER_WORDS = re.compile(
    r"\b(?:books?|classics?|gollancz|harcourt|library|mariner|penguin|"
    r"press|publisher|publishing|riverhead|vintage)\b",
    re.IGNORECASE,
)


class BookMetadataError(RuntimeError):
    """Raised when a download cannot be safely matched or normalized."""


class GoogleBooksUnavailableError(BookMetadataError):
    """Raised when Google Books cannot currently provide metadata."""


def google_books_api_key(explicit: str | None = None) -> str:
    """Resolve the API key from an argument, process env, or project .env."""
    if explicit:
        return explicit
    environment_key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
    if environment_key:
        return environment_key
    try:
        lines = PROJECT_ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == "GOOGLE_BOOKS_API_KEY":
            return value.strip().strip("'\"")
    return ""


@dataclass(frozen=True)
class BookMetadata:
    google_id: str
    title: str
    authors: tuple[str, ...]
    published_date: str
    publisher: str = ""
    description: str = ""
    language: str = ""
    categories: tuple[str, ...] = ()
    isbn_10: tuple[str, ...] = ()
    isbn_13: tuple[str, ...] = ()

    @property
    def year(self) -> str:
        match = _YEAR.search(self.published_date)
        return match.group(1) if match else ""

    @property
    def identifiers(self) -> tuple[str, ...]:
        return self.isbn_13 + self.isbn_10


def _clean_text(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    return " ".join(_CONTROL.sub(" ", text).split())


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", value.casefold()))


def _normalized_isbn(value: str) -> str:
    compact = re.sub(r"[^\dXx]", "", value).upper()
    return compact if len(compact) in {10, 13} else ""


def _opf_rootfile(archive: ZipFile) -> str:
    try:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(f".//{{{_CONTAINER}}}rootfile")
        if rootfile is None:
            rootfile = container.find(".//{*}rootfile")
        path = rootfile.attrib.get("full-path", "") if rootfile is not None else ""
    except (KeyError, ET.ParseError) as error:
        raise BookMetadataError(f"invalid EPUB container: {error}") from error
    if not path:
        raise BookMetadataError("invalid EPUB container: package document is missing")
    return path


def epub_isbns(path: Path) -> tuple[str, ...]:
    """Return distinct ISBN-10/13 identifiers embedded in an EPUB."""
    try:
        with ZipFile(path) as archive:
            package = ET.fromstring(archive.read(_opf_rootfile(archive)))
    except (OSError, KeyError, ET.ParseError, BookMetadataError) as error:
        raise BookMetadataError(f"could not read EPUB metadata: {error}") from error

    found: list[str] = []
    for identifier in package.findall(f".//{{{_DC}}}identifier"):
        raw = _clean_text(identifier.text)
        for candidate in _ISBN.findall(raw):
            isbn = _normalized_isbn(candidate)
            if isbn and isbn not in found:
                found.append(isbn)
    return tuple(found)


def epub_search_hints(path: Path) -> tuple[str, tuple[str, ...]]:
    """Return a usable embedded title/authors pair for catalog searching."""
    try:
        with ZipFile(path) as archive:
            package = ET.fromstring(archive.read(_opf_rootfile(archive)))
    except (OSError, KeyError, ET.ParseError, BookMetadataError):
        return "", ()
    titles = [
        _clean_text(element.text)
        for element in package.findall(f".//{{{_DC}}}title")
        if _clean_text(element.text)
    ]
    authors = tuple(
        author
        for author in (
            _clean_text(element.text)
            for element in package.findall(f".//{{{_DC}}}creator")
        )
        if author and author.casefold() not in {"unknown", "[no data]"}
    )
    title = titles[0] if titles else ""
    if title.casefold() in {"unknown", "[no data]"}:
        title = ""
    return title, authors


def _read_epub_metadata(path: Path) -> BookMetadata:
    """Read EPUB metadata without requiring every canonical field."""
    try:
        with ZipFile(path) as archive:
            package = ET.fromstring(archive.read(_opf_rootfile(archive)))
    except (OSError, KeyError, ET.ParseError, BookMetadataError) as error:
        raise BookMetadataError(f"could not read EPUB metadata: {error}") from error

    def values(local_name: str) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                _clean_text(element.text)
                for element in package.findall(f".//{{{_DC}}}{local_name}")
            )
            if value
        )

    titles = values("title")
    authors = values("creator")
    dates = values("date")
    identifiers = values("identifier")
    isbn_10 = tuple(
        isbn
        for value in identifiers
        if (isbn := _normalized_isbn(value)) and len(isbn) == 10
    )
    isbn_13 = tuple(
        isbn
        for value in identifiers
        if (isbn := _normalized_isbn(value)) and len(isbn) == 13
    )
    google_id = next(
        (
            value.removeprefix("google:")
            for value in identifiers
            if value.startswith("google:")
        ),
        "",
    )
    return BookMetadata(
        google_id=google_id,
        title=titles[0] if titles else "",
        authors=authors,
        published_date=dates[0] if dates else "",
        publisher=(values("publisher") or ("",))[0],
        description=(values("description") or ("",))[0],
        language=(values("language") or ("",))[0],
        categories=values("subject"),
        isbn_10=tuple(dict.fromkeys(isbn_10)),
        isbn_13=tuple(dict.fromkeys(isbn_13)),
    )


def read_epub_metadata(path: Path) -> BookMetadata:
    """Read the descriptive metadata Esbern stores in an EPUB package."""
    metadata = _read_epub_metadata(path)
    if not metadata.title or not metadata.authors or not metadata.year:
        raise BookMetadataError(
            f"EPUB metadata is missing a title, author, or year: {path}"
        )
    return metadata


def _parse_volume(item: object) -> BookMetadata | None:
    if not isinstance(item, dict):
        return None
    info = item.get("volumeInfo")
    if not isinstance(info, dict):
        return None
    title = _MARKETING_SUFFIX.sub("", _clean_text(info.get("title")))
    subtitle = _clean_text(info.get("subtitle"))
    if ":" in subtitle and _MARKETING_WORDS.search(subtitle.rsplit(":", 1)[0]):
        subtitle = subtitle.rsplit(":", 1)[1].strip()
    if subtitle and subtitle.casefold() not in title.casefold():
        title = f"{title}: {subtitle}"
    title = _MARKETING_SUFFIX.sub("", title)
    title_parts = title.split(":")
    kept_parts = [title_parts[0]]
    for part in title_parts[1:]:
        stripped = re.sub(
            r"\s+(?:an? novel|a story)\s*$", "", part.strip(), flags=re.IGNORECASE
        )
        if _GENERIC_SUBTITLE.match(stripped):
            break
        if stripped:
            kept_parts.append(stripped)
    title = ": ".join(kept_parts)
    raw_authors = info.get("authors")
    authors = tuple(
        author
        for author in (_clean_text(value) for value in raw_authors or [])
        if author
    )
    published_date = _clean_text(info.get("publishedDate"))
    if not title or not authors or not _YEAR.search(published_date):
        return None

    isbn_10: list[str] = []
    isbn_13: list[str] = []
    for identifier in info.get("industryIdentifiers") or []:
        if not isinstance(identifier, dict):
            continue
        isbn = _normalized_isbn(_clean_text(identifier.get("identifier")))
        kind = identifier.get("type")
        if kind == "ISBN_10" and isbn:
            isbn_10.append(isbn)
        elif kind == "ISBN_13" and isbn:
            isbn_13.append(isbn)

    categories = tuple(
        category
        for category in (_clean_text(value) for value in info.get("categories") or [])
        if category
    )
    return BookMetadata(
        google_id=_clean_text(item.get("id")),
        title=title,
        authors=authors,
        published_date=published_date,
        publisher=_clean_text(info.get("publisher")),
        description=_clean_text(info.get("description")),
        language=_clean_text(info.get("language")),
        categories=categories,
        isbn_10=tuple(dict.fromkeys(isbn_10)),
        isbn_13=tuple(dict.fromkeys(isbn_13)),
    )


def _request_volumes(query: str, api_key: str) -> list[dict]:
    global _GOOGLE_BACKOFF_REASON, _GOOGLE_BACKOFF_UNTIL

    response: requests.Response | None = None
    with _API_LOCK:
        if time.monotonic() < _GOOGLE_BACKOFF_UNTIL:
            raise GoogleBooksUnavailableError(_GOOGLE_BACKOFF_REASON)
        for attempt in range(3):
            try:
                response = requests.get(
                    GOOGLE_BOOKS_API_URL,
                    params={
                        "q": query,
                        "key": api_key,
                        "maxResults": 10,
                        "printType": "books",
                        "projection": "full",
                    },
                    headers={"User-Agent": GOOGLE_BOOKS_USER_AGENT},
                    timeout=(10, 30),
                )
            except requests.RequestException as error:
                # Exception strings can contain the prepared URL, including
                # the API-key query parameter. Keep credentials out of logs.
                raise GoogleBooksUnavailableError(
                    f"Google Books request failed ({type(error).__name__})"
                ) from error
            if response.status_code == 429 and _daily_quota_exhausted(response):
                break
            if response.status_code != 429 and response.status_code < 500:
                break
            if attempt < 2:
                try:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                except ValueError:
                    retry_after = 0.0
                time.sleep(max(retry_after, 2**attempt))

    assert response is not None
    if response.status_code >= 400:
        message = ""
        try:
            payload = response.json()
            message = _clean_text(payload.get("error", {}).get("message"))
        except (ValueError, AttributeError):
            pass
        detail = f": {message}" if message else ""
        reason = f"Google Books returned HTTP {response.status_code}{detail}"
        if response.status_code == 429:
            _GOOGLE_BACKOFF_REASON = reason
            _GOOGLE_BACKOFF_UNTIL = time.monotonic() + _GOOGLE_BACKOFF_SECONDS
        raise GoogleBooksUnavailableError(reason)
    try:
        payload = response.json()
    except ValueError as error:
        raise GoogleBooksUnavailableError(
            "Google Books returned invalid JSON"
        ) from error
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _daily_quota_exhausted(response: requests.Response) -> bool:
    try:
        payload = response.json()
        error = payload.get("error", {})
        details = error.get("details", [])
    except (ValueError, AttributeError):
        return False
    for detail in details:
        if not isinstance(detail, dict):
            continue
        metadata = detail.get("metadata", {})
        if not isinstance(metadata, dict):
            continue
        if metadata.get("quota_unit") == "1/d/{project}":
            return True
        if metadata.get("quota_limit") == "defaultPerDayPerProject":
            return True
    return False


def _query_score(metadata: BookMetadata, query: str) -> float:
    wanted = _terms(query)
    title_terms = _terms(metadata.title)
    author_terms = _terms(" ".join(metadata.authors))
    candidate = title_terms | author_terms
    if not wanted or not candidate:
        return 0.0
    token_overlap = len(wanted & candidate) / len(wanted | candidate)
    sequence = SequenceMatcher(
        None,
        " ".join(sorted(wanted)),
        " ".join(sorted(candidate)),
    ).ratio()
    title_coverage = (
        len(title_terms & wanted) / len(title_terms) if title_terms else 0.0
    )
    primary_title_terms = _terms(metadata.title.split(":", 1)[0])
    primary_title_coverage = (
        len(primary_title_terms & wanted) / len(primary_title_terms)
        if primary_title_terms
        else 0.0
    )
    author_coverage = (
        len(author_terms & wanted) / len(author_terms) if author_terms else 0.0
    )
    normalized_query = " ".join(re.findall(r"[^\W_]+", query.casefold()))
    exact_author_coverage = (
        sum(
            " ".join(re.findall(r"[^\W_]+", author.casefold())) in normalized_query
            for author in metadata.authors
        )
        / len(metadata.authors)
        if metadata.authors
        else 0.0
    )
    return (
        token_overlap * 0.15
        + sequence * 0.10
        + title_coverage * 0.15
        + primary_title_coverage * 0.25
        + author_coverage * 0.25
        + exact_author_coverage * 0.10
    )


def _author_surnames(authors: tuple[str, ...]) -> set[str]:
    surnames: set[str] = set()
    for author in authors:
        tokens = re.findall(r"[^\W_]+", author.casefold())
        if tokens:
            surnames.add(tokens[-1])
    return surnames


def _meaningful_title_match(title: str, query: str) -> bool:
    """Require title evidence beyond words common to ordinary prose."""
    title_terms = _terms(title) - _TITLE_STOPWORDS
    query_terms = _terms(query) - _TITLE_STOPWORDS
    overlap = title_terms & query_terms
    if not overlap:
        return False
    coverage = len(overlap) / len(title_terms)
    return (
        len(overlap) >= 2 or coverage >= 0.5 or any(len(term) >= 5 for term in overlap)
    )


def _expected_metadata_match(
    metadata: BookMetadata,
    expected_title: str,
    expected_authors: tuple[str, ...],
) -> bool:
    if expected_title:
        expected_terms = _terms(expected_title) - _TITLE_STOPWORDS
        candidate_terms = _terms(metadata.title) - _TITLE_STOPWORDS
        if expected_terms:
            coverage = len(expected_terms & candidate_terms) / len(expected_terms)
            expected_primary = (
                _terms(expected_title.split(":", 1)[0].split(" (", 1)[0])
                - _TITLE_STOPWORDS
            )
            candidate_primary = (
                _terms(metadata.title.split(":", 1)[0].split(" (", 1)[0])
                - _TITLE_STOPWORDS
            )
            primary_coverage = (
                len(expected_primary & candidate_primary) / len(expected_primary)
                if expected_primary
                else 0.0
            )
            if coverage < 0.6 and primary_coverage < 0.8:
                return False
    if expected_authors:
        expected_terms = _terms(" ".join(expected_authors))
        author_matches = [
            len(_terms(author) & expected_terms) / len(_terms(author))
            for author in metadata.authors
            if _terms(author)
        ]
        if not author_matches or max(author_matches) < 0.5:
            return False
    return True


def lookup_google_books(
    query: str,
    *,
    isbns: tuple[str, ...] = (),
    api_key: str | None = None,
    expected_title: str = "",
    expected_authors: tuple[str, ...] = (),
) -> BookMetadata:
    """Resolve a user query/embedded ISBN to one unambiguous Google volume."""
    key = google_books_api_key(api_key)
    if not key:
        raise GoogleBooksUnavailableError(
            "Google Books metadata requires GOOGLE_BOOKS_API_KEY or --google-books-key"
        )

    for isbn in isbns:
        normalized = _normalized_isbn(isbn)
        if not normalized:
            continue
        records = [
            metadata
            for metadata in (
                _parse_volume(item)
                for item in _request_volumes(f"isbn:{normalized}", key)
            )
            if metadata is not None and normalized in metadata.identifiers
        ]
        if records:
            scored_isbn_records = sorted(
                ((_query_score(metadata, query), metadata) for metadata in records),
                key=lambda pair: pair[0],
                reverse=True,
            )
            best_score, best_record = scored_isbn_records[0]
            query_is_isbn = normalized in _normalized_isbn(query)
            title_matches_query = _meaningful_title_match(best_record.title, query)
            expected_match = _expected_metadata_match(
                best_record, expected_title, expected_authors
            )
            if query_is_isbn or (
                best_score >= 0.28 and title_matches_query and expected_match
            ):
                return best_record

    records = [
        metadata
        for metadata in (_parse_volume(item) for item in _request_volumes(query, key))
        if metadata is not None
    ]
    if not records:
        raise BookMetadataError(f'Google Books found no complete match for "{query}"')
    records = [
        metadata
        for metadata in records
        if _expected_metadata_match(metadata, expected_title, expected_authors)
    ]
    if not records:
        raise BookMetadataError(
            f'Google Books found no author/title-verified match for "{query}"'
        )
    scored = sorted(
        ((_query_score(metadata, query), metadata) for metadata in records),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    if best_score < 0.28 or not _meaningful_title_match(best.title, query):
        raise BookMetadataError(f'Google Books results did not closely match "{query}"')
    if len(scored) > 1:
        second_score, second = scored[1]
        same_work = _terms(best.title) == _terms(second.title) and _author_surnames(
            best.authors
        ) == _author_surnames(second.authors)
        if not same_work and best_score - second_score < 0.04:
            raise BookMetadataError(
                f'Google Books returned ambiguous matches for "{query}"'
            )
    return best


def _metadata_element(package: ET.Element) -> ET.Element:
    metadata = package.find(f"{{{_OPF}}}metadata")
    if metadata is None:
        metadata = package.find("{*}metadata")
    if metadata is None:
        raise BookMetadataError("invalid EPUB package: metadata element is missing")
    return metadata


def _replace_dc_values(
    metadata: ET.Element, local_name: str, values: list[str]
) -> None:
    existing = list(metadata.findall(f"{{{_DC}}}{local_name}"))
    insertion_index = list(metadata).index(existing[0]) if existing else len(metadata)
    removed_ids = {
        element.attrib.get("id", "") for element in existing if element.attrib.get("id")
    }
    for element in existing:
        metadata.remove(element)
    for element in list(metadata):
        refines = element.attrib.get("refines", "").lstrip("#")
        if refines and refines in removed_ids:
            metadata.remove(element)
    for offset, value in enumerate(values):
        element = ET.Element(f"{{{_DC}}}{local_name}")
        element.text = value
        if local_name == "creator":
            element.set("id", f"esbern-author-{offset + 1}")
            element.set(f"{{{_OPF}}}role", "aut")
        metadata.insert(insertion_index + offset, element)


def _append_identifier(metadata: ET.Element, value: str, scheme: str) -> None:
    normalized = _normalized_isbn(value) if scheme.startswith("ISBN") else value
    for element in metadata.findall(f"{{{_DC}}}identifier"):
        existing = _clean_text(element.text)
        if scheme.startswith("ISBN"):
            existing = _normalized_isbn(existing)
        if existing == normalized:
            return
    element = ET.SubElement(metadata, f"{{{_DC}}}identifier")
    element.text = value
    element.set(f"{{{_OPF}}}scheme", scheme)


def write_epub_metadata(path: Path, book: BookMetadata) -> None:
    """Atomically update descriptive EPUB package metadata."""
    ET.register_namespace("dc", _DC)
    ET.register_namespace("opf", _OPF)
    ET.register_namespace("dcterms", _DCTERMS)
    try:
        with ZipFile(path) as source:
            rootfile = _opf_rootfile(source)
            package = ET.fromstring(source.read(rootfile))
            metadata = _metadata_element(package)
            _replace_dc_values(metadata, "title", [book.title])
            _replace_dc_values(metadata, "creator", list(book.authors))
            if book.published_date:
                _replace_dc_values(metadata, "date", [book.published_date])
            if book.publisher:
                _replace_dc_values(metadata, "publisher", [book.publisher])
            if book.language:
                _replace_dc_values(metadata, "language", [book.language])
            if book.description:
                _replace_dc_values(metadata, "description", [book.description])
            if book.categories:
                _replace_dc_values(metadata, "subject", list(book.categories))
            for isbn in book.isbn_13:
                _append_identifier(metadata, isbn, "ISBN-13")
            for isbn in book.isbn_10:
                _append_identifier(metadata, isbn, "ISBN-10")
            if book.google_id:
                _append_identifier(metadata, f"google:{book.google_id}", "GOOGLE")
            package_xml = ET.tostring(package, encoding="utf-8", xml_declaration=True)

            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                with ZipFile(temporary, "w") as target:
                    infos = source.infolist()
                    infos.sort(key=lambda info: info.filename != "mimetype")
                    for info in infos:
                        payload = (
                            package_xml
                            if info.filename == rootfile
                            else source.read(info)
                        )
                        if info.filename == "mimetype":
                            mimetype = ZipInfo("mimetype", date_time=info.date_time)
                            mimetype.compress_type = ZIP_STORED
                            mimetype.external_attr = info.external_attr
                            target.writestr(mimetype, payload)
                        else:
                            target.writestr(info, payload)
                shutil.copymode(path, temporary)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, KeyError, ET.ParseError) as error:
        raise BookMetadataError(f"could not update EPUB metadata: {error}") from error


def _filename_component(value: str) -> str:
    value = _clean_text(value).replace(":", " —").translate(_FILENAME_SEPARATORS)
    value = " ".join(_FILENAME_DROP.sub("", value).split()).strip().rstrip(".")
    return value or "Unknown"


def _truncate_utf8(value: str, maximum: int) -> str:
    while len(value.encode("utf-8")) > maximum:
        value = value[:-1].rstrip()
    return value


def canonical_filename(book: BookMetadata, suffix: str) -> str:
    authors = ", ".join(_filename_component(author) for author in book.authors)
    title = _filename_component(book.title)
    ending = f" ({book.year}){suffix.lower()}" if book.year else suffix.lower()
    authors = _truncate_utf8(authors, 160)
    prefix = f"{authors} - "
    available = 240 - len((prefix + ending).encode("utf-8"))
    title = _truncate_utf8(title, max(available, 1))
    return f"{prefix}{title}{ending}"


def _repair_libgen_text(value: str, *, replace_underscores: bool = True) -> str:
    value = value.replace("&_039_", "'").replace("&amp_", "&")
    value = value.replace("_", " ") if replace_underscores else value
    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired.count("�") <= value.count("�"):
            value = repaired
            break
    return _clean_text(value)


def _libgen_author(value: str) -> str:
    cleaned = _repair_libgen_text(value).strip(" ,;")
    if cleaned.count(",") == 1:
        family, given = (part.strip() for part in cleaned.split(",", 1))
        if family and given:
            cleaned = f"{given} {family}"
    return cleaned


def _libgen_filename_metadata(path: Path) -> tuple[str, tuple[str, ...], str]:
    """Extract approximate canonical fields from a common LibGen filename."""
    stem = _repair_libgen_text(path.stem, replace_underscores=False).strip()
    stem = re.sub(r"^(?:\[[^]]+\]\s*)+", "", stem)
    stem = re.sub(r"\s+-\s+libgen\.(?:li|rs|is)\s*$", "", stem, flags=re.IGNORECASE)
    if " - " not in stem:
        return _repair_libgen_text(stem), (), ""

    author_text, title = (part.strip(" ,") for part in stem.split(" - ", 1))
    raw_authors = [
        part for part in re.split(r"\s+_\s+|;\s*|\s+&\s+", author_text) if part.strip()
    ]
    authors = tuple(
        author for author in (_libgen_author(value) for value in raw_authors) if author
    )

    year = ""
    parenthetical = re.search(r"\s*\(([^()]*)\)\s*$", title)
    if parenthetical:
        year_match = _YEAR.search(parenthetical.group(1))
        if year_match:
            year = year_match.group(1)
            title = title[: parenthetical.start()]
    while True:
        parenthetical = re.search(r"\s*\(([^()]*)\)\s*$", title)
        if not parenthetical or not _LIBGEN_PUBLISHER_WORDS.search(
            parenthetical.group(1)
        ):
            break
        title = title[: parenthetical.start()]
    title = _MARKETING_SUFFIX.sub("", _repair_libgen_text(title)).strip(" ,")
    return title, authors, year


def _query_cased_title(title: str, query: str) -> str:
    """Reuse the user's casing when the cleaned title is a plain query phrase."""
    if not title or re.search(r"[^\w\s'’-]", title, flags=re.UNICODE):
        return title
    title_words = re.findall(r"[^\W_]+", title, flags=re.UNICODE)
    query_words = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    wanted = [word.casefold() for word in title_words]
    available = [word.casefold() for word in query_words]
    for index in range(len(available) - len(wanted) + 1):
        if available[index : index + len(wanted)] == wanted:
            return " ".join(query_words[index : index + len(wanted)])
    return title


def infer_libgen_metadata(
    path: Path,
    query: str,
    *,
    expected_title: str = "",
    expected_authors: tuple[str, ...] = (),
) -> BookMetadata:
    """Build best-effort metadata without making a network request."""
    filename_title, filename_authors, filename_year = _libgen_filename_metadata(path)
    embedded = _read_epub_metadata(path) if path.suffix.lower() == ".epub" else None

    title = _clean_text(expected_title) or filename_title
    if not title and embedded is not None:
        title = embedded.title
    if not title:
        title = re.sub(
            r"\b(?:97[89])?\d[\dXx -]{8,16}\b",
            "",
            _repair_libgen_text(query),
        ).strip(" ,-")
    title = _MARKETING_SUFFIX.sub("", title).strip()
    if not title:
        title = _repair_libgen_text(path.stem)
    title = _query_cased_title(title, query)

    authors = filename_authors
    if not authors:
        authors = tuple(
            author
            for author in (_clean_text(value) for value in expected_authors)
            if author
        )
    if not authors and embedded is not None:
        authors = tuple(
            author
            for author in (_clean_text(value) for value in embedded.authors)
            if author and author.casefold() not in {"unknown", "[no data]"}
        )
    if not authors:
        authors = ("Unknown Author",)

    published_date = filename_year
    if not published_date and embedded is not None:
        published_date = embedded.published_date if embedded.year else ""
    if not published_date:
        query_year = _YEAR.search(query)
        published_date = query_year.group(1) if query_year else ""

    return BookMetadata(
        google_id="",
        title=title,
        authors=authors,
        published_date=published_date,
        publisher=embedded.publisher if embedded is not None else "",
        description=embedded.description if embedded is not None else "",
        language=embedded.language if embedded is not None else "",
        categories=embedded.categories if embedded is not None else (),
        isbn_10=embedded.isbn_10 if embedded is not None else (),
        isbn_13=embedded.isbn_13 if embedded is not None else (),
    )


def resolve_book_metadata(
    path: Path,
    query: str,
    *,
    api_key: str | None = None,
    expected_title: str = "",
    expected_authors: tuple[str, ...] = (),
) -> tuple[BookMetadata, str]:
    """Resolve metadata through Google, falling back locally when unavailable."""
    suffix = path.suffix.lower()
    isbns = epub_isbns(path) if suffix == ".epub" else ()
    try:
        return (
            lookup_google_books(
                query,
                isbns=isbns,
                api_key=api_key,
                expected_title=expected_title,
                expected_authors=expected_authors,
            ),
            "google",
        )
    except GoogleBooksUnavailableError:
        return (
            infer_libgen_metadata(
                path,
                query,
                expected_title=expected_title,
                expected_authors=expected_authors,
            ),
            "libgen",
        )


def normalize_download(
    path: Path,
    query: str,
    *,
    api_key: str | None = None,
) -> tuple[Path, BookMetadata]:
    """Look up, embed (EPUB), and rename one downloaded book."""
    suffix = path.suffix.lower()
    book, _source = resolve_book_metadata(path, query, api_key=api_key)
    destination = path.with_name(canonical_filename(book, suffix))
    if suffix == ".epub":
        write_epub_metadata(path, book)
    with _CANONICAL_RENAME_LOCK:
        if destination != path and destination.exists():
            raise BookMetadataError(
                f"canonical filename already exists: {destination.name}; "
                f"download remains at {path}"
            )
        if destination != path:
            path.replace(destination)
    return destination.resolve(), book
