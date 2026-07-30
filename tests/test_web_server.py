from __future__ import annotations

import os
from unittest.mock import patch
from zipfile import ZipFile

from click.testing import CliRunner
from fastapi.testclient import TestClient

from esbern.cli import main
from esbern.downloader import DownloadedBook
from esbern.server_library import catalog, cover, install_books
from esbern.web_server import create_app


def test_catalog_recursively_lists_books_and_skips_internal_files(tmp_path) -> None:
    fiction = tmp_path / "Fiction"
    fiction.mkdir()
    (fiction / "Ursula Le Guin - The Dispossessed (1974).epub").write_bytes(
        b"not a real epub"
    )
    (tmp_path / "Paper.pdf").write_bytes(b"not a real pdf")
    hidden = tmp_path / ".esbern"
    hidden.mkdir()
    (hidden / "Ignored.pdf").write_bytes(b"ignored")

    result = catalog(tmp_path)

    assert result["count"] == 2
    assert [book["title"] for book in result["books"]] == [
        "Paper",
        "The Dispossessed",
    ]
    assert result["books"][1]["authors"] == ("Ursula Le Guin",)
    assert result["books"][1]["folder"] == "Fiction"


def test_cover_uses_deterministic_svg_when_no_embedded_image_exists(tmp_path) -> None:
    (tmp_path / "Octavia Butler - Parable of the Sower (1993).pdf").write_bytes(
        b"not a real pdf"
    )
    book = catalog(tmp_path)["books"][0]

    image = cover(tmp_path, book["id"])

    assert image.content_type == "image/svg+xml"
    assert b"Parable of the Sower" in image.data
    assert image.version == book["cover_version"]


def test_cover_reads_embedded_epub_cover_art(tmp_path) -> None:
    path = tmp_path / "Covered Book.epub"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>""",
        )
        archive.writestr(
            "OEBPS/content.opf",
            """<package xmlns="http://www.idpf.org/2007/opf">
<manifest><item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/></manifest>
</package>""",
        )
        archive.writestr("OEBPS/cover.png", b"\x89PNG\r\n\x1a\ncover-bytes")
    book = catalog(tmp_path)["books"][0]

    image = cover(tmp_path, book["id"])

    assert image.content_type == "image/png"
    assert image.data == b"\x89PNG\r\n\x1a\ncover-bytes"


def test_root_page_is_an_image_only_grid(tmp_path) -> None:
    (tmp_path / "Book One.pdf").write_bytes(b"one")
    (tmp_path / "Book Two.epub").write_bytes(b"two")
    client = TestClient(create_app(tmp_path))

    response = client.get("/")

    assert response.status_code == 200
    assert response.text.count("<img ") == 2
    assert "<button" not in response.text
    assert "<form" not in response.text
    assert "<nav" not in response.text


def test_catalog_and_cover_routes(tmp_path) -> None:
    (tmp_path / "A Book.pdf").write_bytes(b"pdf")
    client = TestClient(create_app(tmp_path))

    listing = client.get("/api/books")
    book_id = listing.json()["books"][0]["id"]
    image = client.get(f"/api/books/{book_id}/cover")

    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/svg+xml"
    assert image.headers["x-content-type-options"] == "nosniff"


@patch("esbern.web_server.install_books")
def test_single_install_api_enables_automatic_sync(install, tmp_path) -> None:
    install.return_value = {
        "downloaded": [{"query": "A Book"}],
        "skipped": [],
        "failed": [],
        "sync": {"ok": True},
    }
    client = TestClient(create_app(tmp_path))

    response = client.post("/api/books", json={"query": "A Book"})

    assert response.status_code == 201
    assert install.call_args.args[0] == tmp_path
    assert install.call_args.args[1]["queries"] == ["A Book"]


@patch("esbern.web_server.install_books")
def test_bulk_api_accepts_the_contents_of_a_text_file(install, tmp_path) -> None:
    install.return_value = {
        "downloaded": [{"query": "First Book"}, {"query": "Second Book"}],
        "skipped": [],
        "failed": [],
        "sync": {"ok": True},
    }
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/api/books/bulk?jobs=6&source=auto",
        content="First Book\n\nSecond Book\n",
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 201
    payload = install.call_args.args[1]
    assert payload["queries"] == ["First Book", "Second Book"]
    assert payload["jobs"] == 6
    assert payload["source"] == "auto"


@patch("esbern.web_server.install_books")
def test_mutation_routes_honor_optional_bearer_token(install, tmp_path) -> None:
    install.return_value = {
        "downloaded": [],
        "skipped": [{"query": "A Book"}],
        "failed": [],
        "sync": {"ok": True},
    }
    client = TestClient(create_app(tmp_path))
    with patch.dict(os.environ, {"ESBERN_API_TOKEN": "secret-token"}):
        unauthorized = client.post("/api/books", json={"query": "A Book"})
        authorized = client.post(
            "/api/books",
            json={"query": "A Book"},
            headers={"Authorization": "Bearer secret-token"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code != 401
    assert install.call_count == 1


@patch("esbern.server_library._sync")
@patch("esbern.server_library.find_existing_book", return_value=None)
@patch("esbern.server_library.download_book")
def test_download_batch_runs_one_sync_after_all_downloads(
    download, _find_existing, sync, tmp_path
) -> None:
    def downloaded(query, destination, **_kwargs):
        path = destination / f"{query}.epub"
        path.write_bytes(query.encode())
        return DownloadedBook(query, path, "epub")

    download.side_effect = downloaded
    sync.return_value = {"ok": True, "stats": {}, "events": []}
    with patch("esbern.server_library.google_books_api_key", return_value="key"):
        result = install_books(
            tmp_path,
            {"queries": ["First Book", "Second Book"], "jobs": 2},
        )

    assert len(result["downloaded"]) == 2
    sync.assert_called_once_with(tmp_path, workers=4)


@patch("esbern.server_library._sync")
@patch("esbern.server_library.download_book")
def test_download_skips_a_matching_book_in_a_nested_folder_and_still_syncs(
    download, sync, tmp_path
) -> None:
    nested = tmp_path / "Fiction"
    nested.mkdir()
    existing = nested / "Ursula Le Guin - The Dispossessed (1974).epub"
    existing.write_bytes(b"existing")
    sync.return_value = {"ok": True, "stats": {}, "events": []}
    with patch("esbern.server_library.google_books_api_key", return_value="key"):
        result = install_books(
            tmp_path,
            {"queries": ["The Dispossessed Ursula Le Guin"]},
        )

    download.assert_not_called()
    assert result["skipped"][0]["relpath"] == "Fiction/" + existing.name
    sync.assert_called_once_with(tmp_path, workers=4)


@patch("esbern.web_server.synchronize")
def test_sync_api_runs_the_full_library_sync(synchronize, tmp_path) -> None:
    synchronize.return_value = {"ok": True, "stats": {}, "events": []}
    client = TestClient(create_app(tmp_path))

    response = client.post("/api/sync", json={"workers": 3})

    assert response.status_code == 200
    synchronize.assert_called_once_with(tmp_path, workers=3)


@patch("esbern.cli.serve_web")
def test_serve_command_starts_python_server(serve_web, tmp_path) -> None:
    result = CliRunner().invoke(
        main,
        ["serve", "--path", str(tmp_path), "--host", "127.0.0.1", "--port", "8123"],
    )

    assert result.exit_code == 0, result.output
    serve_web.assert_called_once_with(tmp_path, hostname="127.0.0.1", port=8123)
