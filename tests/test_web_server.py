from __future__ import annotations

import os
import time
from unittest.mock import patch
from zipfile import ZipFile

from click.testing import CliRunner
from fastapi.testclient import TestClient

from esbern.cli import main
from esbern.downloader import DownloadedBook
from esbern.server_library import (
    _sync_roots,
    catalog,
    cover,
    install_books,
    search_catalog,
)
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


def test_search_catalog_matches_words_across_title_and_author(tmp_path) -> None:
    (tmp_path / "Ursula K. Le Guin - The Dispossessed (1974).epub").write_bytes(b"book")
    (tmp_path / "Octavia Butler - Kindred (1979).epub").write_bytes(b"book")

    result = search_catalog(tmp_path, "ursula dispossessed")

    assert result["count"] == 1
    assert result["returned"] == 1
    assert result["books"][0]["title"] == "The Dispossessed"


def test_search_api_and_full_catalog_api(tmp_path) -> None:
    (tmp_path / "Ursula K. Le Guin - The Dispossessed (1974).epub").write_bytes(b"book")
    (tmp_path / "Octavia Butler - Kindred (1979).epub").write_bytes(b"book")
    client = TestClient(create_app(tmp_path))

    full_catalog = client.get("/api/books")
    search = client.get("/api/books/search", params={"q": "Octavia Kindred"})

    assert full_catalog.status_code == 200
    assert full_catalog.json()["count"] == 2
    assert search.status_code == 200
    assert search.json()["count"] == 1
    assert search.json()["books"][0]["title"] == "Kindred"


def test_chatgpt_action_schema_exposes_read_and_queued_write_tools(tmp_path) -> None:
    client = TestClient(create_app(tmp_path))

    response = client.get("/integrations/chatgpt/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert "search by ISBN" in schema["info"]["description"]
    assert schema["paths"]["/api/books"]["get"]["operationId"] == "listLibrary"
    assert schema["paths"]["/api/books/search"]["get"]["operationId"] == "searchLibrary"
    queue = schema["paths"]["/api/jobs/books"]["post"]
    assert queue["operationId"] == "queueBook"
    assert "ISBN" in queue["description"]
    assert queue["x-openai-isConsequential"] is True
    assert queue["security"] == [{"BearerAuth": []}]


@patch("esbern.server_jobs.install_books")
def test_phone_client_can_queue_and_poll_an_authenticated_book_job(
    install, tmp_path
) -> None:
    install.return_value = {
        "downloaded": [{"query": "A Book"}],
        "skipped": [],
        "failed": [],
        "sync": {"ok": True},
        "catalog": {"count": 3, "books": []},
    }
    headers = {"Authorization": "Bearer secret-token"}

    with (
        patch.dict(os.environ, {"ESBERN_API_TOKEN": "secret-token"}),
        TestClient(create_app(tmp_path)) as client,
    ):
        unauthorized = client.post("/api/jobs/books", json={"query": "A Book"})
        queued = client.post(
            "/api/jobs/books", json={"query": "A Book"}, headers=headers
        )
        job_id = queued.json()["id"]
        deadline = time.monotonic() + 2
        while True:
            current = client.get(f"/api/jobs/{job_id}", headers=headers)
            if current.json()["status"] in {"succeeded", "failed"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

    assert unauthorized.status_code == 401
    assert queued.status_code == 202
    assert queued.headers["location"] == f"/api/jobs/{job_id}"
    assert current.status_code == 200
    assert current.json()["status"] == "succeeded"
    assert current.json()["result"]["catalog_count"] == 3
    assert "catalog" not in current.json()["result"]
    assert install.call_args.args[1]["queries"] == ["A Book"]


def test_job_status_rejects_invalid_or_missing_ids(tmp_path) -> None:
    client = TestClient(create_app(tmp_path))

    invalid = client.get("/api/jobs/not-a-job")
    missing = client.get("/api/jobs/0123456789abcdef0123456789abcdef")

    assert invalid.status_code == 400
    assert missing.status_code == 404


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


def test_existing_child_states_are_independent_sync_roots(tmp_path) -> None:
    for name in ("Books", "papers"):
        state = tmp_path / name / ".esbern" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text("{}")

    assert _sync_roots(tmp_path) == (tmp_path / "Books", tmp_path / "papers")


@patch("esbern.server_library._sync")
@patch("esbern.server_library.find_existing_book", return_value=None)
@patch("esbern.server_library.download_book")
def test_multi_scope_downloads_default_to_books(
    download, _find_existing, sync, tmp_path
) -> None:
    for name in ("Books", "papers"):
        state = tmp_path / name / ".esbern" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text("{}")

    def downloaded(query, destination, **_kwargs):
        path = destination / f"{query}.epub"
        path.write_bytes(query.encode())
        return DownloadedBook(query, path, "epub")

    download.side_effect = downloaded
    sync.return_value = {"ok": True, "stats": {}, "events": []}
    with patch("esbern.server_library.google_books_api_key", return_value="key"):
        result = install_books(tmp_path, {"queries": ["A New Book"]})

    assert download.call_args.args[1] == tmp_path / "Books"
    assert result["downloaded"][0]["relpath"] == "Books/A New Book.epub"
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
