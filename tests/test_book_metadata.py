from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from requests import RequestException

from esbern.book_metadata import (
    BookMetadata,
    BookMetadataError,
    canonical_filename,
    epub_isbns,
    lookup_google_books,
    normalize_download,
)

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0"
 xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="EPUB/package.opf"
      media-type="application/oebps-package+xml" />
  </rootfiles>
</container>
"""

PACKAGE = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
 unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:isbn:9781234567897</dc:identifier>
    <dc:title id="old-title">Bad Download Name</dc:title>
    <meta refines="#old-title" property="title-type">main</meta>
    <dc:creator id="old-author">Wrong Author</dc:creator>
    <meta refines="#old-author" property="role" scheme="marc:relators">aut</meta>
    <dc:date>0101-01-01</dc:date>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml" />
  </manifest>
  <spine><itemref idref="chapter" /></spine>
</package>
"""


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


def _volume(*, title="Computing", subtitle="Past and Future"):
    return {
        "id": "google-volume",
        "volumeInfo": {
            "title": title,
            "subtitle": subtitle,
            "authors": ["Ada Lovelace", "Grace Hopper"],
            "publisher": "Example Press",
            "publishedDate": "2024-03-05",
            "description": "A useful description.",
            "language": "en",
            "categories": ["Computers", "History"],
            "industryIdentifiers": [
                {"type": "ISBN_13", "identifier": "9781234567897"},
                {"type": "ISBN_10", "identifier": "123456789X"},
            ],
        },
    }


def _epub(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml", CONTAINER, compress_type=ZIP_DEFLATED
        )
        archive.writestr("EPUB/package.opf", PACKAGE, compress_type=ZIP_DEFLATED)
        archive.writestr(
            "EPUB/chapter.xhtml", "<html><body>Kept</body></html>",
            compress_type=ZIP_DEFLATED,
        )


@patch("esbern.book_metadata.requests.get")
def test_normalizes_epub_from_exact_embedded_isbn(get, tmp_path) -> None:
    source = tmp_path / "messy (Publisher) - libgen.li.epub"
    _epub(source)
    get.return_value = FakeResponse({"items": [_volume()]})

    path, metadata = normalize_download(
        source, "Computing Ada Lovelace", api_key="secret-key"
    )

    assert path.name == (
        "Ada Lovelace, Grace Hopper - Computing — Past and Future (2024).epub"
    )
    assert metadata.publisher == "Example Press"
    assert epub_isbns(path) == ("9781234567897", "123456789X")
    assert get.call_args.kwargs["params"]["q"] == "isbn:9781234567897"
    assert get.call_args.kwargs["params"]["key"] == "secret-key"

    with ZipFile(path) as archive:
        assert archive.infolist()[0].filename == "mimetype"
        assert archive.infolist()[0].compress_type == ZIP_STORED
        assert b"Kept" in archive.read("EPUB/chapter.xhtml")
        package = archive.read("EPUB/package.opf").decode("utf-8")
    assert "Computing: Past and Future" in package
    assert "Ada Lovelace" in package
    assert "Grace Hopper" in package
    assert "Example Press" in package
    assert "A useful description." in package
    assert "Wrong Author" not in package
    assert "#old-author" not in package


@patch("esbern.book_metadata.requests.get")
def test_query_fallback_rejects_marketing_filename_fluff(get) -> None:
    volume = _volume(
        title="Pachinko (National Book Award Finalist)", subtitle=""
    )
    get.return_value = FakeResponse({"items": [volume]})

    result = lookup_google_books(
        "Pachinko Ada Lovelace Grace Hopper", api_key="secret"
    )

    assert result.title == "Pachinko"


@patch("esbern.book_metadata.requests.get")
def test_corrupt_embedded_isbn_does_not_override_user_query(get) -> None:
    wrong = _volume(title="Good Omens", subtitle="")
    right = _volume(title="Simulacron-3", subtitle="")
    get.side_effect = [
        FakeResponse({"items": [wrong]}),
        FakeResponse({"items": [right]}),
    ]

    result = lookup_google_books(
        "Simulacron-3 Ada Lovelace Grace Hopper",
        isbns=("9781234567897",),
        api_key="secret",
    )

    assert result.title == "Simulacron-3"
    assert get.call_count == 2


@patch("esbern.book_metadata.requests.get")
def test_stopword_only_title_overlap_does_not_override_user_query(get) -> None:
    wrong = _volume(title="The Good Omens", subtitle="")
    right = _volume(title="The Simulacron-3", subtitle="")
    get.side_effect = [
        FakeResponse({"items": [wrong]}),
        FakeResponse({"items": [right]}),
    ]

    result = lookup_google_books(
        "The Simulacron-3 Ada Lovelace Grace Hopper",
        isbns=("9781234567897",),
        api_key="secret",
    )

    assert result.title == "The Simulacron-3"
    assert get.call_count == 2


@patch("esbern.book_metadata.requests.get")
def test_author_match_beats_derivative_title_that_mentions_the_authors(get) -> None:
    correct = _volume(title="Abundance", subtitle="How We Build a Better Future")
    derivative = {
        "id": "derivative",
        "volumeInfo": {
            "title": "Abundance Formula",
            "subtitle": "How Ada Lovelace and Grace Hopper See the Future",
            "authors": ["Lucas Vale"],
            "publishedDate": "2025",
        },
    }
    get.return_value = FakeResponse({"items": [derivative, correct]})

    result = lookup_google_books(
        "Abundance Ada Lovelace Grace Hopper", api_key="secret"
    )

    assert result.google_id == "google-volume"


@patch("esbern.book_metadata.requests.get")
def test_exact_isbn_query_does_not_require_title_overlap(get) -> None:
    get.return_value = FakeResponse(
        {"items": [_volume(title="A Completely Different Title", subtitle="")]}
    )

    result = lookup_google_books(
        "9781234567897",
        isbns=("9781234567897",),
        api_key="secret",
    )

    assert result.title == "A Completely Different Title"
    assert get.call_count == 1


def test_requires_google_books_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GOOGLE_BOOKS_API_KEY", raising=False)
    monkeypatch.setattr(
        "esbern.book_metadata.PROJECT_ENV_PATH", tmp_path / "missing.env"
    )
    with pytest.raises(BookMetadataError, match="GOOGLE_BOOKS_API_KEY"):
        lookup_google_books("A Book")


@patch("esbern.book_metadata.requests.get")
def test_google_request_errors_do_not_expose_the_api_key(get) -> None:
    get.side_effect = RequestException(
        "failed https://www.googleapis.com/books/v1/volumes?key=private-key"
    )

    with pytest.raises(BookMetadataError) as raised:
        lookup_google_books("A Book", api_key="private-key")

    assert "private-key" not in str(raised.value)


def test_filename_is_bounded_and_keeps_required_shape() -> None:
    book = BookMetadata(
        google_id="id",
        title="A Very Long Title " * 30,
        authors=("An Extremely Long Author Name " * 10,),
        published_date="2026",
    )

    filename = canonical_filename(book, ".EPUB")

    assert len(filename.encode("utf-8")) <= 240
    assert " - " in filename
    assert filename.endswith(" (2026).epub")


def test_concurrent_normalization_does_not_overwrite_canonical_file(
    tmp_path,
) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"first download")
    second.write_bytes(b"second download")
    book = BookMetadata(
        google_id="id",
        title="Shared Title",
        authors=("Shared Author",),
        published_date="2026",
    )
    canonical_name = "Shared Author - Shared Title (2026).pdf"
    lookup_barrier = threading.Barrier(2)
    second_collision_check = threading.Event()
    collision_check_count = 0
    count_lock = threading.Lock()
    original_exists = Path.exists

    def concurrent_lookup(*args, **kwargs):
        lookup_barrier.wait(timeout=1)
        return book

    def coordinated_exists(path: Path) -> bool:
        nonlocal collision_check_count
        if path.name != canonical_name:
            return original_exists(path)
        existed = original_exists(path)
        with count_lock:
            collision_check_count += 1
            if collision_check_count == 2:
                second_collision_check.set()
        if not existed:
            second_collision_check.wait(timeout=0.2)
        return existed

    with (
        patch(
            "esbern.book_metadata.lookup_google_books",
            side_effect=concurrent_lookup,
        ),
        patch.object(Path, "exists", coordinated_exists),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        futures = [
            executor.submit(normalize_download, source, "Shared Title")
            for source in (first, second)
        ]
        successes = []
        errors = []
        for future in futures:
            try:
                successes.append(future.result())
            except BookMetadataError as error:
                errors.append(error)

    destination = tmp_path / canonical_name
    assert len(successes) == 1
    assert len(errors) == 1
    assert "canonical filename already exists" in str(errors[0])
    assert destination.read_bytes() in {b"first download", b"second download"}
    assert sum(source.exists() for source in (first, second)) == 1
