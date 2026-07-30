from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from esbern import xochitl
from esbern.cli import main
from esbern.state import FileEntry, State
from esbern.sync import (
    SyncEvent,
    SyncStats,
    _ensure_root_collection,
    _ensure_subfolder,
    _walk_local,
    pull,
    sync,
)
from esbern.tags import TagStore


@dataclass
class _MetadataEntry:
    text: str
    mtime: float = 1.0


class FakeRemarkable:
    def __init__(self, entries: dict[str, str] | None = None):
        self.entries = {
            f"{uid}.metadata": _MetadataEntry(text)
            for uid, text in (entries or {}).items()
        }
        self.writes: list[tuple[str, str]] = []
        self.payloads: dict[str, bytes] = {}
        self.uploads: list[tuple[Path, str]] = []
        self.downloads: list[tuple[str, Path]] = []
        self.restart_count = 0

    def remote_path(self, path: str) -> str:
        return path

    def list_xochitl_metadata(self) -> list[tuple[str, float]]:
        return [
            (path.removesuffix(".metadata"), entry.mtime)
            for path, entry in self.entries.items()
            if path.endswith(".metadata")
        ]

    def get_text(self, path: str) -> str:
        return self.entries[path].text

    def exists(self, path: str) -> bool:
        return path in self.entries or path in self.payloads

    def put_text(self, path: str, text: str) -> None:
        self.entries[path] = _MetadataEntry(text)
        self.writes.append((path, text))

    def stat_mtime(self, path: str) -> float:
        return self.entries[path].mtime

    def stat_size(self, path: str) -> int | None:
        payload = self.payloads.get(path)
        return len(payload) if payload is not None else None

    def file_sha256(self, path: str) -> str | None:
        payload = self.payloads.get(path)
        return hashlib.sha256(payload).hexdigest() if payload is not None else None

    def annotation_dir_mtime(self, uid: str) -> float:
        return 0.0

    def get_file(self, remote: str, local, callback=None) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        payload = self.payloads[remote]
        local.write_bytes(payload)
        self.downloads.append((remote, local))
        if callback:
            callback(len(payload), len(payload))

    def put_file(self, local: Path, remote: str, callback=None) -> None:
        payload = local.read_bytes()
        self.payloads[remote] = payload
        self.uploads.append((local, remote))
        if callback:
            callback(len(payload), len(payload))

    def restart_xochitl(self) -> None:
        self.restart_count += 1


def test_local_walk_never_follows_file_or_directory_symlinks(tmp_path) -> None:
    local_root = tmp_path / "Library"
    local_root.mkdir()
    (local_root / "Local.epub").write_bytes(b"local")

    outside = tmp_path / "Outside"
    outside.mkdir()
    outside_book = outside / "Private.pdf"
    outside_book.write_bytes(b"private")
    (local_root / "Linked File.pdf").symlink_to(outside_book)
    (local_root / "Linked Directory").symlink_to(outside, target_is_directory=True)

    assert list(_walk_local(local_root)) == [(Path("Local.epub"), False)]


@pytest.mark.parametrize(
    "unsafe_relpath",
    ["../outside.epub", "/tmp/outside.epub", "nested/../../outside.epub", "C:/outside.epub", "nested\\outside.epub"],
)
def test_state_rejects_paths_that_can_escape_sync_root(
    tmp_path, unsafe_relpath
) -> None:
    local_root = tmp_path / "Library"
    state_dir = local_root / ".esbern"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "files": {
                    unsafe_relpath: {"uuid": "book", "file_type": "epub"}
                }
            }
        )
    )

    with pytest.raises(ValueError, match="Unsafe file path"):
        State.load(local_root)


def test_first_sync_adopts_existing_root_case_insensitively() -> None:
    rm = FakeRemarkable(
        {
            "existing-books": xochitl.collection_metadata("Books"),
            "nested-books": xochitl.collection_metadata("books", parent="some-parent"),
            "document": xochitl.document_metadata("books"),
        }
    )
    state = State()
    stats = SyncStats()

    uid = _ensure_root_collection(rm, state, "books", stats)

    assert uid == "existing-books"
    assert state.root_uuid == "existing-books"
    assert stats.folders_created == 0
    assert rm.writes == []


def test_first_sync_ignores_trashed_matching_root() -> None:
    trashed = json.loads(xochitl.collection_metadata("Books"))
    trashed["parent"] = "trash"
    trashed["deleted"] = True
    rm = FakeRemarkable(
        {
            "active": xochitl.collection_metadata("Books"),
            "trashed": json.dumps(trashed),
        }
    )

    uid = _ensure_root_collection(rm, State(), "books", SyncStats())

    assert uid == "active"


def test_sync_does_not_restore_or_reuse_saved_trashed_root() -> None:
    trashed = json.loads(xochitl.collection_metadata("Books"))
    trashed["parent"] = "trash"
    trashed["deleted"] = True
    rm = FakeRemarkable(
        {
            "active": xochitl.collection_metadata("Books"),
            "trashed": json.dumps(trashed),
        }
    )
    state = State(
        root_uuid="trashed",
        files={"Old.epub": FileEntry(uuid="old", file_type="epub")},
    )

    events: list[SyncEvent] = []
    uid = _ensure_root_collection(
        rm, state, "Books", SyncStats(), reporter=events.append
    )

    assert uid == "active"
    assert state.root_uuid == "active"
    assert state.files == {}
    assert json.loads(rm.get_text("trashed.metadata"))["deleted"] is True
    assert rm.writes == []
    assert any(event.action == "trashed root ignored" for event in events)


def test_sync_does_not_reuse_saved_root_outside_local_directory_scope() -> None:
    rm = FakeRemarkable(
        {
            "books": xochitl.collection_metadata("Books"),
            "personal": xochitl.collection_metadata("Personal"),
        }
    )
    state = State(
        root_uuid="personal",
        files={"Private.epub": FileEntry(uuid="private", file_type="epub")},
    )

    events: list[SyncEvent] = []
    uid = _ensure_root_collection(
        rm, state, "Books", SyncStats(), reporter=events.append
    )

    assert uid == "books"
    assert state.root_uuid == "books"
    assert state.files == {}
    assert (
        xochitl.parse_metadata(rm.get_text("personal.metadata"))["name"] == "Personal"
    )
    assert rm.writes == []
    assert any(event.action == "out-of-scope root ignored" for event in events)


def test_sync_scope_is_named_after_selected_local_directory(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Papers"
    local_root.mkdir()
    rm = FakeRemarkable(
        {
            "papers": xochitl.collection_metadata("Papers"),
            "paper": xochitl.document_metadata("Scoped Paper", parent="papers"),
            "books": xochitl.collection_metadata("Books"),
            "book": xochitl.document_metadata("Outside Book", parent="books"),
        }
    )
    rm.entries["paper.content"] = _MetadataEntry(xochitl.document_content("pdf"))
    rm.payloads["paper.pdf"] = b"scoped paper"
    rm.entries["book.content"] = _MetadataEntry(xochitl.document_content("epub"))
    rm.payloads["book.epub"] = b"outside scope"

    store = TagStore()
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    monkeypatch.setattr("esbern.sync.categorize", lambda *args: [])

    events: list[SyncEvent] = []
    sync(rm, local_root, restart=False, reporter=events.append)

    assert (local_root / "Scoped Paper.pdf").read_bytes() == b"scoped paper"
    assert not (local_root / "Outside Book.epub").exists()
    assert State.load(local_root).root_uuid == "papers"
    assert rm.uploads == []
    assert any(
        event.kind == "phase" and "reMarkable/Papers only" in event.action
        for event in events
    )
    assert not any("Outside Book" in event.item for event in events)


@pytest.mark.parametrize("workers", [1, 2])
def test_sync_reuploads_local_book_with_new_uuid_when_tracked_document_moved_outside_root(
    tmp_path, monkeypatch, workers
) -> None:
    local_root = tmp_path / "Library"
    local_root.mkdir()
    local_book = local_root / "Moved Book.epub"
    local_book.write_bytes(b"local payload")
    moved_metadata = xochitl.document_metadata("Moved Book", parent="other-root")
    moved_content = xochitl.document_content("epub")
    rm = FakeRemarkable(
        {
            "root": xochitl.collection_metadata("Library"),
            "other-root": xochitl.collection_metadata("Other"),
            "moved": moved_metadata,
        }
    )
    rm.entries["moved.content"] = _MetadataEntry(moved_content)
    rm.payloads["moved.epub"] = b"remote payload"
    State(
        root_uuid="root",
        files={
            local_book.name: FileEntry(
                uuid="moved",
                file_type="epub",
                size=local_book.stat().st_size,
                mtime=local_book.stat().st_mtime,
                remote_mtime=1.0,
                tags=["Saved Tag"],
            )
        },
    ).save(local_root)

    store = TagStore()
    store.set(local_book.name, ["Saved Tag"])
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    if workers > 1:
        rm.cfg = object()

        class WorkerRemarkable:
            def __init__(self, cfg) -> None:
                assert cfg is rm.cfg

            def connect(self) -> None:
                pass

            def close(self) -> None:
                pass

            def __getattr__(self, name):
                return getattr(rm, name)

        monkeypatch.setattr("esbern.sync.Remarkable", WorkerRemarkable)

    events: list[SyncEvent] = []
    stats = sync(
        rm,
        local_root,
        restart=False,
        reporter=events.append,
        workers=workers,
    )

    saved = State.load(local_root).files[local_book.name]
    assert saved.uuid != "moved"
    assert stats.files_uploaded == 1
    assert stats.files_updated == 0
    assert rm.get_text("moved.metadata") == moved_metadata
    assert rm.get_text("moved.content") == moved_content
    assert rm.payloads["moved.epub"] == b"remote payload"
    assert all(remote != "moved.epub" for _, remote in rm.uploads)
    assert any(
        event.action == "stale file mapping pruned; local copy will upload"
        for event in events
    )


def test_sync_prunes_state_when_file_is_missing_locally_and_from_active_root(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Library"
    local_root.mkdir()
    State(
        root_uuid="root",
        files={"Missing.epub": FileEntry(uuid="gone", file_type="epub")},
    ).save(local_root)
    rm = FakeRemarkable({"root": xochitl.collection_metadata("Library")})
    store = TagStore()
    store.set("Missing.epub", ["Stale Tag"])
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)

    events: list[SyncEvent] = []
    stats = sync(rm, local_root, restart=False, reporter=events.append)

    assert State.load(local_root).files == {}
    assert store.get("Missing.epub") == []
    assert stats.files_uploaded == 0
    assert rm.uploads == []
    assert any(
        event.action == "stale file mapping pruned; missing on both sides"
        for event in events
    )


def test_first_sync_refuses_ambiguous_existing_roots() -> None:
    rm = FakeRemarkable(
        {
            "first": xochitl.collection_metadata("Books"),
            "second": xochitl.collection_metadata("Books"),
        }
    )

    with pytest.raises(click.ClickException, match="More than one"):
        _ensure_root_collection(rm, State(), "books", SyncStats())

    assert rm.writes == []


@pytest.mark.parametrize(
    "stale_reason",
    ["moved", "trashed", "deleted", "renamed", "wrong-type"],
)
def test_push_replaces_stale_child_folder_mapping(stale_reason) -> None:
    raw = json.loads(xochitl.collection_metadata("Shelf", parent="root"))
    if stale_reason == "moved":
        raw["parent"] = "other-root"
    elif stale_reason == "trashed":
        raw["parent"] = "trash"
    elif stale_reason == "deleted":
        raw["deleted"] = True
    elif stale_reason == "renamed":
        raw["visibleName"] = "Different Shelf"
    else:
        raw["type"] = "DocumentType"
    original = json.dumps(raw)
    rm = FakeRemarkable({"stale": original})
    state = State(root_uuid="root", folders={"Shelf": "stale"})
    stats = SyncStats()
    events: list[SyncEvent] = []

    uid = _ensure_subfolder(
        rm,
        state,
        Path("Shelf"),
        "root",
        stats,
        reporter=events.append,
    )

    assert uid != "stale"
    assert state.folders["Shelf"] == uid
    assert rm.get_text("stale.metadata") == original
    assert xochitl.parse_metadata(rm.get_text(f"{uid}.metadata")) == {
        "name": "Shelf",
        "parent": "root",
        "type": "CollectionType",
        "deleted": False,
        "tags": [],
    }
    assert stats.folders_created == 1
    assert any(event.action == "stale folder mapping replaced" for event in events)


def test_pull_adopts_existing_root_and_never_writes_remote(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "books"
    local_root.mkdir()
    trashed = json.loads(xochitl.collection_metadata("Books"))
    trashed["parent"] = "trash"
    rm = FakeRemarkable(
        {
            "root": xochitl.collection_metadata("Textbooks"),
            "old-root": json.dumps(trashed),
            "book": xochitl.document_metadata("Remote Book", parent="root"),
            "other-root": xochitl.collection_metadata("Personal"),
            "other-book": xochitl.document_metadata(
                "Outside Book", parent="other-root"
            ),
        }
    )
    rm.entries["book.content"] = _MetadataEntry(
        xochitl.document_content("epub"), mtime=2.0
    )
    rm.payloads["book.epub"] = b"remote epub"
    rm.entries["other-book.content"] = _MetadataEntry(
        xochitl.document_content("epub"), mtime=2.0
    )
    rm.payloads["other-book.epub"] = b"outside scope"

    store = TagStore()
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    monkeypatch.setattr("esbern.sync.categorize", lambda *args: ["Fiction"])

    State(
        root_uuid="old-root",
        files={"Old Book.epub": FileEntry(uuid="old-book", file_type="epub")},
    ).save(local_root)

    events: list[SyncEvent] = []
    stats = pull(rm, local_root, remote_name="Textbooks", reporter=events.append)

    assert (local_root / "Remote Book.epub").read_bytes() == b"remote epub"
    assert not (local_root / "Outside Book.epub").exists()
    saved_state = State.load(local_root)
    assert saved_state.root_uuid == "root"
    assert set(saved_state.files) == {"Remote Book.epub"}
    assert stats.files_pulled == 1
    assert stats.tags_assigned == 1
    assert rm.writes == []
    assert any(
        event.kind == "item"
        and event.action == "downloading new"
        and event.item == "Remote Book.epub"
        for event in events
    )
    assert not any("Outside Book" in event.item for event in events)
    assert any(
        event.kind == "phase" and "reMarkable/Textbooks only" in event.action
        for event in events
    )
    assert any(
        event.kind == "transfer"
        and event.item == "Remote Book.epub"
        and event.current == len(b"remote epub")
        and event.total == len(b"remote epub")
        for event in events
    )
    assert any(
        event.kind == "item"
        and event.action == "downloaded"
        and event.item.startswith("Remote Book.epub")
        for event in events
    )


def test_sync_pulls_before_push_and_device_wins_name_collision(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Books"
    local_root.mkdir()
    local_book = local_root / "Shared Title.epub"
    local_book.write_bytes(b"local edition")
    State(
        root_uuid="root",
        files={
            local_book.name: FileEntry(
                uuid="stale-book",
                file_type="epub",
                size=len(b"local edition"),
                mtime=local_book.stat().st_mtime,
            )
        },
    ).save(local_root)
    rm = FakeRemarkable(
        {
            "root": xochitl.collection_metadata("Books"),
            "book": xochitl.document_metadata("Shared Title", parent="root"),
        }
    )
    rm.entries["book.content"] = _MetadataEntry(xochitl.document_content("epub"))
    rm.payloads["book.epub"] = b"device edition"

    store = TagStore()
    store.set(local_book.name, ["Local Only"])
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    monkeypatch.setattr("esbern.sync.categorize", lambda *args: [])

    events: list[SyncEvent] = []
    stats = sync(rm, local_root, restart=False, reporter=events.append)

    assert local_book.read_bytes() == b"device edition"
    backups = [
        path
        for path in (local_root / ".esbern" / "sync-collision-trash").rglob("*")
        if path.is_file()
    ]
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"local edition"
    assert rm.uploads == []
    assert stats.files_pulled == 1
    assert stats.conflicts == 1
    assert store.get(local_book.name) == []
    assert State.load(local_root).files[local_book.name].uuid == "book"
    pull_phase = next(
        index
        for index, event in enumerate(events)
        if event.kind == "phase" and event.phase == "pull"
    )
    push_phase = next(
        index
        for index, event in enumerate(events)
        if event.kind == "phase" and event.phase == "push"
    )
    assert pull_phase < push_phase
    assert any(event.action == "name collision; device wins" for event in events)


def test_sync_links_identical_existing_book_without_downloading(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Library"
    local_root.mkdir()
    local_book = local_root / "Already Here.epub"
    local_book.write_bytes(b"identical book payload")
    rm = FakeRemarkable(
        {
            "root": xochitl.collection_metadata("Library"),
            "book": xochitl.document_metadata("Already Here", parent="root"),
        }
    )
    rm.entries["book.content"] = _MetadataEntry(xochitl.document_content("epub"))
    rm.payloads["book.epub"] = b"identical book payload"

    store = TagStore()
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    monkeypatch.setattr("esbern.sync.categorize", lambda *args: [])

    events: list[SyncEvent] = []
    stats = sync(rm, local_root, restart=False, reporter=events.append)

    assert local_book.read_bytes() == b"identical book payload"
    assert rm.downloads == []
    assert rm.uploads == []
    assert stats.files_linked == 1
    assert stats.files_pulled == 0
    assert State.load(local_root).files[local_book.name].uuid == "book"
    assert any(
        event.action == "already present; linked without download" for event in events
    )
    assert any(
        event.kind == "phase"
        and event.phase == "push"
        and "1 already associated, 0 local-only books to upload" in event.action
        for event in events
    )
    assert not (local_root / ".esbern" / "sync-collision-trash").exists()


def test_interrupted_sync_checkpoints_each_completed_book(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Library"
    local_root.mkdir()
    rm = FakeRemarkable(
        {
            "root": xochitl.collection_metadata("Library"),
            "first": xochitl.document_metadata("First", parent="root"),
            "second": xochitl.document_metadata("Second", parent="root"),
        }
    )
    for uid in ("first", "second"):
        rm.entries[f"{uid}.content"] = _MetadataEntry(xochitl.document_content("epub"))
        rm.payloads[f"{uid}.epub"] = f"{uid} payload".encode()

    store = TagStore()
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    monkeypatch.setattr("esbern.sync.categorize", lambda *args: [])

    real_get_file = rm.get_file
    attempts = 0

    def fail_second_download(remote, local, callback=None):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("connection interrupted")
        return real_get_file(remote, local, callback)

    rm.get_file = fail_second_download
    with pytest.raises(RuntimeError, match="connection interrupted"):
        sync(rm, local_root, restart=False)

    assert len(State.load(local_root).files) == 1
    assert len(rm.downloads) == 1

    rm.get_file = real_get_file
    sync(rm, local_root, restart=False)

    assert len(State.load(local_root).files) == 2
    assert len(rm.downloads) == 2


def test_sync_uploads_with_parallel_connections_and_checkpoints_each_book(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Library"
    local_root.mkdir()
    for index in range(6):
        (local_root / f"Book {index}.epub").write_bytes(f"payload {index}".encode())

    rm = FakeRemarkable({"root": xochitl.collection_metadata("Library")})
    rm.cfg = object()
    active = 0
    most_active = 0
    activity_lock = Lock()
    connections = 0

    class WorkerRemarkable:
        def __init__(self, cfg) -> None:
            assert cfg is rm.cfg

        def connect(self) -> None:
            nonlocal connections
            with activity_lock:
                connections += 1

        def close(self) -> None:
            pass

        def put_file(self, local, remote, callback=None) -> None:
            nonlocal active, most_active
            with activity_lock:
                active += 1
                most_active = max(most_active, active)
            try:
                time.sleep(0.04)
                rm.put_file(local, remote, callback)
            finally:
                with activity_lock:
                    active -= 1

        def __getattr__(self, name):
            return getattr(rm, name)

    store = TagStore()
    monkeypatch.setattr("esbern.sync.Remarkable", WorkerRemarkable)
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    monkeypatch.setattr("esbern.sync.categorize", lambda *args: [])

    events: list[SyncEvent] = []
    stats = sync(
        rm,
        local_root,
        restart=False,
        reporter=events.append,
        workers=3,
    )

    assert most_active >= 2
    assert connections == 3
    assert stats.files_uploaded == 6
    assert len(rm.uploads) == 6
    assert len(State.load(local_root).files) == 6
    assert any("3 parallel SSH workers" in event.action for event in events)


def test_local_update_preserves_device_metadata_and_content_fields(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Library"
    local_root.mkdir()
    local_book = local_root / "Annotated.epub"
    local_book.write_bytes(b"locally updated payload")

    metadata = json.loads(
        xochitl.document_metadata("Annotated", parent="root", tags=["Device Tag"])
    )
    metadata.update(
        {
            "pinned": True,
            "lastOpened": "123456",
            "deviceOnlyField": {"keep": True},
        }
    )
    content = json.dumps(
        {
            "fileType": "epub",
            "pageCount": 2,
            "pages": ["page-one", "page-two"],
            "deviceOnlyField": {"keep": True},
        }
    )
    rm = FakeRemarkable(
        {
            "root": xochitl.collection_metadata("Library"),
            "book": json.dumps(metadata),
        }
    )
    rm.entries["book.content"] = _MetadataEntry(content)
    rm.payloads["book.epub"] = b"old payload"
    State(
        root_uuid="root",
        files={
            local_book.name: FileEntry(
                uuid="book",
                file_type="epub",
                size=len(b"old payload"),
                mtime=local_book.stat().st_mtime,
                remote_mtime=1.0,
                tags=["Device Tag"],
            )
        },
    ).save(local_root)

    store = TagStore()
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    monkeypatch.setattr("esbern.sync.categorize", lambda *args: [])

    stats = sync(rm, local_root, restart=False)

    updated = json.loads(rm.get_text("book.metadata"))
    assert updated["pinned"] is True
    assert updated["lastOpened"] == "123456"
    assert updated["deviceOnlyField"] == {"keep": True}
    assert [tag["name"] for tag in updated["tags"]] == ["Device Tag"]
    assert rm.get_text("book.content") == content
    assert rm.payloads["book.epub"] == b"locally updated payload"
    assert not any(path == "book.content" for path, _ in rm.writes)
    assert stats.files_updated == 1


def test_sync_resumes_from_checkpoint_without_reuploading_completed_book(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Library"
    local_root.mkdir()
    local_book = local_root / "Already Uploaded.epub"
    local_book.write_bytes(b"checkpointed payload")
    rm = FakeRemarkable(
        {
            "root": xochitl.collection_metadata("Library"),
            "book": xochitl.document_metadata("Already Uploaded", parent="root"),
        }
    )
    rm.entries["book.content"] = _MetadataEntry(xochitl.document_content("epub"))
    rm.payloads["book.epub"] = b"checkpointed payload"
    State(
        root_uuid="root",
        files={
            local_book.name: FileEntry(
                uuid="book",
                file_type="epub",
                size=local_book.stat().st_size,
                mtime=local_book.stat().st_mtime,
                remote_mtime=1.0,
                tags=["Fiction"],
            )
        },
    ).save(local_root)

    store = TagStore()
    store.set(local_book.name, ["Fiction"])
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)

    events: list[SyncEvent] = []
    stats = sync(
        rm,
        local_root,
        restart=False,
        reporter=events.append,
        workers=3,
    )

    assert stats.files_uploaded == 0
    assert stats.files_pulled == 0
    assert rm.uploads == []
    assert State.load(local_root).files[local_book.name].uuid == "book"
    assert any(event.action == "resume checkpoint loaded" for event in events)


def test_sync_refreshes_document_service_when_interrupted_after_upload(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Library"
    local_root.mkdir()
    (local_root / "Uploaded Before Failure.epub").write_bytes(b"payload")
    rm = FakeRemarkable({"root": xochitl.collection_metadata("Library")})

    store = TagStore()
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    monkeypatch.setattr("esbern.sync.categorize", lambda *args: [])

    def fail_after_upload(*args, **kwargs) -> None:
        raise RuntimeError("stopped after upload")

    monkeypatch.setattr("esbern.sync._backfill_tags", fail_after_upload)

    with pytest.raises(RuntimeError, match="stopped after upload"):
        sync(rm, local_root, restart=True)

    assert len(State.load(local_root).files) == 1
    assert rm.restart_count == 1


def test_sync_deduplicates_identical_local_file_before_push(
    tmp_path, monkeypatch
) -> None:
    local_root = tmp_path / "Books"
    local_root.mkdir()
    local_duplicate = local_root / "Local Filename.epub"
    local_duplicate.write_bytes(b"same bytes")
    rm = FakeRemarkable(
        {
            "root": xochitl.collection_metadata("Books"),
            "book": xochitl.document_metadata("Remote Filename", parent="root"),
        }
    )
    rm.entries["book.content"] = _MetadataEntry(xochitl.document_content("epub"))
    rm.payloads["book.epub"] = b"same bytes"

    store = TagStore()
    store.set(local_duplicate.name, ["Duplicate"])
    monkeypatch.setattr("esbern.sync.TagStore.load", lambda: store)
    monkeypatch.setattr("esbern.sync.TagStore.save", lambda self: None)
    monkeypatch.setattr("esbern.sync.categorize", lambda *args: [])

    stats = sync(rm, local_root, restart=False)

    assert not local_duplicate.exists()
    assert (local_root / "Remote Filename.epub").read_bytes() == b"same bytes"
    recovered = [
        path
        for path in (local_root / ".esbern" / "dedup-trash").rglob("*")
        if path.is_file()
    ]
    assert len(recovered) == 1
    assert recovered[0].read_bytes() == b"same bytes"
    assert stats.duplicates_removed == 1
    assert store.get(local_duplicate.name) == []
    assert rm.uploads == []


def test_pull_command_creates_named_remote_folder_under_destination(
    tmp_path, monkeypatch
) -> None:
    captured = {}

    class _Connection:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return None

    def fake_pull(rm, root, remote_name=None, reporter=None):
        captured["root"] = root
        captured["remote_name"] = remote_name
        root.mkdir(parents=True)
        reporter(
            SyncEvent(
                kind="item",
                phase="pull",
                action="downloaded",
                item="A Textbook.pdf",
                current=1,
                total=1,
            )
        )
        return SyncStats()

    monkeypatch.setattr(
        "esbern.cli.config.load",
        lambda: SimpleNamespace(user="root", host="remarkable"),
    )
    monkeypatch.setattr("esbern.cli.connected", lambda cfg: _Connection())
    monkeypatch.setattr("esbern.cli.run_pull", fake_pull)

    result = CliRunner().invoke(main, ["pull", "Textbooks", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert captured == {
        "root": Path(tmp_path).resolve() / "Textbooks",
        "remote_name": "Textbooks",
    }
    assert (tmp_path / "Textbooks").is_dir()
    assert "[1/1] downloaded: A Textbook.pdf" in result.output
