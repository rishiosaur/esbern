from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch
from zipfile import ZipFile

from click.testing import CliRunner
from fastapi.testclient import TestClient

from esbern.book_metadata import BookMetadata
from esbern.cli import main
from esbern.downloader import DownloadedBook
from esbern.library_metadata import LibraryMetadataPlan
from esbern.server_library import (
    _sync_roots,
    catalog,
    cover,
    install_books,
    search_catalog,
)
from esbern.server_library import normalize_book as normalize_catalog_book
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


def test_catalog_parses_a_clean_fallback_filename_without_year(tmp_path) -> None:
    (tmp_path / "Octavia Butler - Kindred.epub").write_bytes(b"book")

    book = catalog(tmp_path)["books"][0]

    assert book["title"] == "Kindred"
    assert book["authors"] == ("Octavia Butler",)
    assert book["year"] == ""


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
    sync = schema["paths"]["/api/jobs/sync"]["post"]
    assert sync["operationId"] == "queueLibrarySync"
    assert sync["x-openai-isConsequential"] is True
    assert sync["security"] == [{"BearerAuth": []}]
    push = schema["paths"]["/api/jobs/push"]["post"]
    assert push["operationId"] == "queueLibraryPush"
    pull = schema["paths"]["/api/jobs/pull"]["post"]
    assert pull["operationId"] == "queueLibraryPull"
    assert schema["paths"]["/api/jobs/{job_id}"]["get"]["operationId"] == "getJob"


@patch("esbern.server_jobs.install_books")
def test_phone_client_can_queue_and_poll_an_authenticated_book_job(
    install, tmp_path
) -> None:
    def installed(_root, _payload, *, progress_callback):
        progress_callback("Starting LibGen EPUB search", "A Book")
        progress_callback("Metadata saved", "A Book.epub")
        progress_callback("reMarkable push complete", "1 file deployed")
        return {
            "downloaded": [{"query": "A Book"}],
            "skipped": [],
            "failed": [],
            "push": {"ok": True},
            "catalog": {"count": 3, "books": []},
        }

    install.side_effect = installed
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
    assert current.json()["progress"]["message"] == "Book installation finished"
    messages = [event["message"] for event in current.json()["progress_events"]]
    assert "Starting LibGen EPUB search" in messages
    assert "Metadata saved" in messages
    assert "reMarkable push complete" in messages
    assert install.call_args.args[1]["queries"] == ["A Book"]
    assert callable(install.call_args.kwargs["progress_callback"])


@patch("esbern.server_jobs.synchronize")
def test_phone_client_can_queue_and_poll_an_authenticated_sync_job(
    synchronize, tmp_path
) -> None:
    def synced(_root, *, workers, progress_callback):
        progress_callback("reMarkable sync: pull", "checking 12 documents")
        progress_callback("reMarkable sync complete", "2 file changes")
        return {"ok": True, "stats": {"files_pulled": 1, "files_uploaded": 1}}

    synchronize.side_effect = synced
    headers = {"Authorization": "Bearer secret-token"}

    with (
        patch.dict(os.environ, {"ESBERN_API_TOKEN": "secret-token"}),
        TestClient(create_app(tmp_path)) as client,
    ):
        unauthorized = client.post("/api/jobs/sync", json={"workers": 3})
        queued = client.post(
            "/api/jobs/sync",
            json={"workers": 3},
            headers=headers,
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
    assert current.json()["type"] == "sync_library"
    assert current.json()["status"] == "succeeded"
    assert current.json()["result"]["stats"]["files_uploaded"] == 1
    assert current.json()["progress"]["message"] == "Library sync finished"
    synchronize.assert_called_once()
    assert synchronize.call_args.args == (tmp_path,)
    assert synchronize.call_args.kwargs["workers"] == 3
    assert callable(synchronize.call_args.kwargs["progress_callback"])


@patch("esbern.server_jobs.push_library")
def test_phone_client_can_queue_a_one_way_push(push_library, tmp_path) -> None:
    def pushed(_root, *, workers, progress_callback):
        progress_callback("reMarkable push: push", "uploaded: A Book.epub")
        return {"ok": True, "stats": {"files_uploaded": 1}}

    push_library.side_effect = pushed
    headers = {"Authorization": "Bearer secret-token"}
    with (
        patch.dict(os.environ, {"ESBERN_API_TOKEN": "secret-token"}),
        TestClient(create_app(tmp_path)) as client,
    ):
        queued = client.post(
            "/api/jobs/push",
            json={"workers": 3},
            headers=headers,
        )
        job_id = queued.json()["id"]
        deadline = time.monotonic() + 2
        while True:
            current = client.get(f"/api/jobs/{job_id}", headers=headers)
            if current.json()["status"] in {"succeeded", "failed"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

    assert queued.status_code == 202
    assert current.json()["type"] == "push_library"
    assert current.json()["result"]["stats"]["files_uploaded"] == 1
    push_library.assert_called_once()
    assert push_library.call_args.kwargs["workers"] == 3


@patch("esbern.server_jobs.pull_library")
def test_phone_client_can_queue_a_one_way_pull(pull_library, tmp_path) -> None:
    def pulled(_root, *, progress_callback):
        progress_callback("reMarkable pull: pull", "downloaded: A Book.epub")
        return {"ok": True, "stats": {"files_pulled": 1}}

    pull_library.side_effect = pulled
    headers = {"Authorization": "Bearer secret-token"}
    with (
        patch.dict(os.environ, {"ESBERN_API_TOKEN": "secret-token"}),
        TestClient(create_app(tmp_path)) as client,
    ):
        queued = client.post("/api/jobs/pull", headers=headers)
        job_id = queued.json()["id"]
        deadline = time.monotonic() + 2
        while True:
            current = client.get(f"/api/jobs/{job_id}", headers=headers)
            if current.json()["status"] in {"succeeded", "failed"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

    assert queued.status_code == 202
    assert current.json()["type"] == "pull_library"
    assert current.json()["result"]["stats"]["files_pulled"] == 1
    pull_library.assert_called_once()


@patch("esbern.server_jobs.normalize_book")
def test_phone_client_can_queue_one_book_normalization(normalize, tmp_path) -> None:
    def normalized(_root, *, book_id, query, workers, progress_callback):
        progress_callback("Normalization: fallback", "Books/Messy.epub")
        return {
            "ok": True,
            "book_id": book_id,
            "mode": "local",
            "new_relpath": "Books/Clean.epub",
        }

    normalize.side_effect = normalized
    headers = {"Authorization": "Bearer secret-token"}
    with (
        patch.dict(os.environ, {"ESBERN_API_TOKEN": "secret-token"}),
        TestClient(create_app(tmp_path)) as client,
    ):
        queued = client.post(
            "/api/jobs/normalize",
            json={
                "book_id": "0123456789abcdef01234567",
                "query": "Clean Book",
                "workers": 2,
            },
            headers=headers,
        )
        job_id = queued.json()["id"]
        deadline = time.monotonic() + 2
        while True:
            current = client.get(f"/api/jobs/{job_id}", headers=headers)
            if current.json()["status"] in {"succeeded", "failed"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

    assert queued.status_code == 202
    assert current.json()["type"] == "normalize_book"
    assert current.json()["status"] == "succeeded"
    assert current.json()["result"]["new_relpath"] == "Books/Clean.epub"
    assert normalize.call_args.kwargs["book_id"] == "0123456789abcdef01234567"
    assert normalize.call_args.kwargs["query"] == "Clean Book"
    assert normalize.call_args.kwargs["workers"] == 2


def test_job_status_rejects_invalid_or_missing_ids(tmp_path) -> None:
    client = TestClient(create_app(tmp_path))

    invalid = client.get("/api/jobs/not-a-job")
    missing = client.get("/api/jobs/0123456789abcdef0123456789abcdef")

    assert invalid.status_code == 400
    assert missing.status_code == 404


@patch("esbern.web_server.install_books")
def test_single_install_api_enables_automatic_push(install, tmp_path) -> None:
    install.return_value = {
        "downloaded": [{"query": "A Book"}],
        "skipped": [],
        "failed": [],
        "push": {"ok": True},
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
        "push": {"ok": True},
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
        "push": {"ok": True},
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


@patch("esbern.server_library._push")
@patch("esbern.server_library.find_existing_book", return_value=None)
@patch("esbern.server_library.download_book")
def test_download_batch_runs_one_targeted_push_after_all_downloads(
    download, _find_existing, push, tmp_path
) -> None:
    def downloaded(query, destination, **_kwargs):
        path = destination / f"{query}.epub"
        path.write_bytes(query.encode())
        return DownloadedBook(query, path, "epub")

    download.side_effect = downloaded
    push.return_value = {"ok": True, "stats": {}, "events": []}
    with patch("esbern.server_library.google_books_api_key", return_value="key"):
        result = install_books(
            tmp_path,
            {"queries": ["First Book", "Second Book"], "jobs": 2},
        )

    assert len(result["downloaded"]) == 2
    assert result["push"]["ok"] is True
    push.assert_called_once_with(
        tmp_path,
        workers=4,
        paths=[
            tmp_path / "First Book.epub",
            tmp_path / "Second Book.epub",
        ],
    )


@patch("esbern.server_library.normalize_local_book")
def test_normalize_untracked_book_is_local_only(normalize_local, tmp_path) -> None:
    source = tmp_path / "Messy Book.epub"
    source.write_bytes(b"book")
    book_id = catalog(tmp_path)["books"][0]["id"]
    destination = tmp_path / "Author - Clean Book (2024).epub"
    metadata = BookMetadata(
        google_id="",
        title="Clean Book",
        authors=("Author",),
        published_date="2024",
    )
    normalize_local.return_value = (destination, metadata, "libgen")

    with patch("esbern.server_library.google_books_api_key", return_value=""):
        result = normalize_catalog_book(
            tmp_path,
            book_id=book_id,
            query="Clean Book Author",
        )

    assert result["mode"] == "local"
    assert result["new_relpath"] == destination.name
    assert result["metadata_source"] == "libgen"
    assert result["remarkable_updated"] is False
    normalize_local.assert_called_once_with(
        source,
        "Clean Book Author",
        api_key="",
    )


@patch("esbern.server_library.apply_library_metadata")
@patch("esbern.server_library.plan_book_metadata")
@patch("esbern.server_library.connected")
def test_normalize_tracked_book_preserves_remote_identity(
    connected, plan_book, apply_metadata, tmp_path
) -> None:
    source = tmp_path / "Messy Book.epub"
    source.write_bytes(b"book")
    book_id = catalog(tmp_path)["books"][0]["id"]
    metadata = BookMetadata(
        google_id="google-id",
        title="Clean Book",
        authors=("Author",),
        published_date="2024",
    )
    plan_book.return_value = LibraryMetadataPlan(
        source.name,
        "Author - Clean Book (2024).epub",
        metadata,
    )
    apply_metadata.return_value = SimpleNamespace(files_updated=1, files_renamed=1)
    remarkable = MagicMock()
    connected.return_value.__enter__.return_value = remarkable

    with (
        patch(
            "esbern.server_library.State.load",
            return_value=SimpleNamespace(files={source.name: object()}),
        ),
        patch(
            "esbern.server_library.config.load",
            return_value=SimpleNamespace(restart_xochitl=True),
        ),
        patch("esbern.server_library.google_books_api_key", return_value="key"),
    ):
        result = normalize_catalog_book(
            tmp_path,
            book_id=book_id,
            query="Clean Book Author",
            workers=2,
        )

    assert result["mode"] == "tracked"
    assert result["remarkable_updated"] is True
    plan_book.assert_called_once_with(
        tmp_path,
        source.name,
        api_key="key",
        query="Clean Book Author",
        reporter=ANY,
    )
    apply_metadata.assert_called_once()
    assert apply_metadata.call_args.args[1:] == (
        tmp_path,
        [plan_book.return_value],
    )
    assert apply_metadata.call_args.kwargs["workers"] == 2
    remarkable.restart_xochitl.assert_called_once_with()


@patch("esbern.server_library._push")
@patch("esbern.server_library.find_existing_book", return_value=None)
@patch("esbern.server_library.download_book")
def test_server_allows_keyless_download_metadata_fallback(
    download, _find_existing, push, tmp_path
) -> None:
    path = tmp_path / "Fallback Book.epub"
    path.write_bytes(b"book")
    download.return_value = DownloadedBook("Fallback Book", path, "epub")
    push.return_value = {"ok": True, "stats": {}, "events": []}

    with patch("esbern.server_library.google_books_api_key", return_value=""):
        result = install_books(tmp_path, {"queries": ["Fallback Book"]})

    assert len(result["downloaded"]) == 1
    assert result["failed"] == []
    assert download.call_args.kwargs["google_books_api_key"] == ""
    assert download.call_args.kwargs["enrich_metadata"] is True
    push.assert_called_once_with(
        tmp_path,
        workers=4,
        paths=[path],
    )


def test_existing_child_states_are_independent_sync_roots(tmp_path) -> None:
    for name in ("Books", "papers"):
        state = tmp_path / name / ".esbern" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text("{}")

    assert _sync_roots(tmp_path) == (tmp_path / "Books", tmp_path / "papers")


@patch("esbern.server_library._push")
@patch("esbern.server_library.find_existing_book", return_value=None)
@patch("esbern.server_library.download_book")
def test_multi_scope_downloads_default_to_books(
    download, _find_existing, push, tmp_path
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
    push.return_value = {"ok": True, "stats": {}, "events": []}
    with patch("esbern.server_library.google_books_api_key", return_value="key"):
        result = install_books(tmp_path, {"queries": ["A New Book"]})

    assert download.call_args.args[1] == tmp_path / "Books"
    assert result["downloaded"][0]["relpath"] == "Books/A New Book.epub"
    push.assert_called_once_with(
        tmp_path,
        workers=4,
        paths=[tmp_path / "Books" / "A New Book.epub"],
    )


@patch("esbern.server_library._push")
@patch("esbern.server_library.download_book")
def test_download_skips_a_matching_book_without_contacting_remarkable(
    download, push, tmp_path
) -> None:
    nested = tmp_path / "Fiction"
    nested.mkdir()
    existing = nested / "Ursula Le Guin - The Dispossessed (1974).epub"
    existing.write_bytes(b"existing")
    with patch("esbern.server_library.google_books_api_key", return_value="key"):
        result = install_books(
            tmp_path,
            {"queries": ["The Dispossessed Ursula Le Guin"]},
        )

    download.assert_not_called()
    assert result["skipped"][0]["relpath"] == "Fiction/" + existing.name
    assert result["push"] is None
    push.assert_not_called()


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
