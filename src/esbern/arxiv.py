"""Search and download papers from arXiv's public API."""

from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_USER_AGENT = "esbern/0.1 (personal research downloader)"
_ATOM = {"atom": "http://www.w3.org/2005/Atom"}
_ARXIV_ID = re.compile(
    r"(?i)(?:arxiv\s*:\s*|https?://arxiv\.org/(?:abs|pdf)/)?"
    r"((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?)"
    r"(?:\.pdf)?$"
)
_DOI = re.compile(r"(?i)(?:https?://doi\.org/|doi\s*:\s*)?(10\.\d{4,9}/\S+)$")
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_API_LOCK = threading.Lock()
_last_api_request = 0.0

ProgressCallback = Callable[[str | None, str | None], None]


class ArxivDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArxivPaper:
    arxiv_id: str
    title: str
    authors: tuple[str, ...]
    pdf_url: str


@dataclass(frozen=True)
class ArxivDownload:
    path: Path
    paper: ArxivPaper


def looks_like_paper_identifier(query: str) -> bool:
    query = query.strip()
    return bool(_ARXIV_ID.fullmatch(query) or _DOI.fullmatch(query))


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", value.casefold()))


def _api_params(query: str) -> dict[str, str | int]:
    arxiv_match = _ARXIV_ID.fullmatch(query.strip())
    if arxiv_match:
        return {"id_list": arxiv_match.group(1), "max_results": 1}
    doi_match = _DOI.fullmatch(query.strip())
    if doi_match:
        return {"search_query": f"doi:{doi_match.group(1)}", "max_results": 3}
    search_terms = re.findall(r"[^\W_]+", query)
    return {
        "search_query": " AND ".join(f'all:"{term}"' for term in search_terms),
        "start": 0,
        "max_results": 5,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }


def _query_api(params: dict[str, str | int]) -> str:
    """Make one serialized API call while honoring arXiv's 3-second limit."""
    global _last_api_request
    with _API_LOCK:
        for attempt in range(3):
            delay = 3.0 - (time.monotonic() - _last_api_request)
            if delay > 0:
                time.sleep(delay)
            response = requests.get(
                ARXIV_API_URL,
                params=params,
                headers={"User-Agent": ARXIV_USER_AGENT},
                timeout=(10, 30),
            )
            _last_api_request = time.monotonic()
            if response.status_code != 429 or attempt == 2:
                response.raise_for_status()
                return response.text
            try:
                retry_after = float(response.headers.get("Retry-After", "3"))
            except ValueError:
                retry_after = 3.0
            time.sleep(max(3.0, retry_after))
    raise ArxivDownloadError("arXiv API retry loop ended unexpectedly")


def _parse_feed(xml_text: str) -> list[ArxivPaper]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise ArxivDownloadError(f"arXiv returned invalid metadata: {error}") from error

    papers: list[ArxivPaper] = []
    for entry in root.findall("atom:entry", _ATOM):
        raw_id = (entry.findtext("atom:id", default="", namespaces=_ATOM)
                  .rsplit("/abs/", 1)[-1].strip())
        title = " ".join(
            entry.findtext("atom:title", default=raw_id, namespaces=_ATOM).split()
        )
        authors = tuple(
            " ".join(author.findtext("atom:name", default="", namespaces=_ATOM).split())
            for author in entry.findall("atom:author", _ATOM)
        )
        pdf_url = ""
        for link in entry.findall("atom:link", _ATOM):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break
        if raw_id and pdf_url:
            papers.append(ArxivPaper(
                arxiv_id=raw_id,
                title=title,
                authors=tuple(author for author in authors if author),
                pdf_url=pdf_url.replace("http://", "https://", 1),
            ))
    return papers


def search_arxiv(query: str) -> ArxivPaper:
    papers = _parse_feed(_query_api(_api_params(query)))
    if not papers:
        raise ArxivDownloadError("no matching arXiv paper was found")

    if looks_like_paper_identifier(query):
        return papers[0]

    query_terms = _terms(query)
    for paper in papers:
        searchable = _terms(f"{paper.title} {' '.join(paper.authors)}")
        if len(query_terms) >= 2 and query_terms <= searchable:
            return paper
    raise ArxivDownloadError("arXiv results did not closely match the title/author query")


def _truncate_utf8(value: str, maximum: int) -> str:
    while len(value.encode("utf-8")) > maximum:
        value = value[:-1].rstrip()
    return value


def _safe_filename(paper: ArxivPaper) -> str:
    compact_id = paper.arxiv_id.rsplit("/", 1)[-1]
    year_match = re.match(r"(\d{2})\d{2}", compact_id)
    if not year_match:
        raise ArxivDownloadError(
            f"cannot determine publication year from arXiv ID {paper.arxiv_id}"
        )
    short_year = int(year_match.group(1))
    year = 1900 + short_year if short_year >= 91 else 2000 + short_year
    authors = ", ".join(paper.authors) or "Unknown"
    authors = _BAD_FILENAME.sub("_", authors).strip().rstrip(".")
    authors = _truncate_utf8(authors, 160).rstrip(" ,") or "Unknown"
    title = paper.title.replace(":", " —")
    title = _BAD_FILENAME.sub("_", title).strip().rstrip(".") or paper.arxiv_id
    ending = f" ({year}).pdf"
    prefix = f"{authors} - "
    available = 240 - len((prefix + ending).encode("utf-8"))
    title = _truncate_utf8(title, max(available, 1))
    return f"{prefix}{title}{ending}"


def fetch_arxiv(query: str, directory: Path,
                 progress_callback: ProgressCallback | None = None) -> ArxivDownload:
    if progress_callback:
        progress_callback("Searching arXiv…", None)
    try:
        paper = search_arxiv(query)
    except (requests.RequestException, ArxivDownloadError) as error:
        raise ArxivDownloadError(str(error)) from error

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _safe_filename(paper)
    if progress_callback:
        progress_callback("Downloading from arXiv…", path.name)
    try:
        response = requests.get(
            paper.pdf_url,
            headers={"User-Agent": ARXIV_USER_AGENT},
            timeout=(10, 120),
            stream=True,
        )
        response.raise_for_status()
        size = 0
        with path.open("wb") as stream:
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                stream.write(chunk)
                size += len(chunk)
                if progress_callback:
                    progress_callback(None, f"{path.name} · {size / 1024 / 1024:.1f} MB")
        with path.open("rb") as stream:
            is_pdf = stream.read(5) == b"%PDF-"
        if not is_pdf:
            path.unlink(missing_ok=True)
            raise ArxivDownloadError("arXiv response was not a PDF")
    except (OSError, requests.RequestException) as error:
        path.unlink(missing_ok=True)
        raise ArxivDownloadError(f"could not download the arXiv PDF: {error}") from error
    return ArxivDownload(path=path, paper=paper)
