from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from esbern import tags as tags_module
from esbern import xochitl
from esbern.book_metadata import BookMetadata, BookMetadataError
from esbern.library_metadata import (
    LibraryMetadataPlan,
    apply_library_metadata,
)
from esbern.state import FileEntry, State
from esbern.tags import TagStore

BOOK = BookMetadata(
    google_id="google-book",
    title="Safe Systems",
    authors=("Ada Lovelace",),
    published_date="2024-01-02",
)
OLD_RELPATH = "Messy Book.epub"
NEW_RELPATH = "Ada Lovelace - Safe Systems (2024).epub"


class FakeRemarkable:
    def __init__(self, *, file_type: str = "epub") -> None:
        self.metadata = {
            "root": xochitl.collection_metadata("Library"),
            "book": xochitl.document_metadata("Messy Book", parent="root"),
        }
        self.metadata_mtime = {"root": 1.0, "book": 1.0}
        self.payloads = {f"book.{file_type}": b"original"}
        self.put_text_hook: Callable[[str, str], None] | None = None
        self.text_writes: list[tuple[str, str]] = []
        self.file_writes: list[tuple[str, bytes]] = []
        self.restart_count = 0

    def remote_path(self, path: str) -> str:
        return path

    def read_xochitl_metadata(self) -> list[tuple[str, float, str]]:
        return [
            (uid, self.metadata_mtime[uid], raw) for uid, raw in self.metadata.items()
        ]

    def exists(self, path: str) -> bool:
        if path.endswith(".metadata"):
            return path.removesuffix(".metadata") in self.metadata
        return path in self.payloads

    def get_text(self, path: str) -> str:
        return self.metadata[path.removesuffix(".metadata")]

    def put_text(self, path: str, text: str) -> None:
        if self.put_text_hook is not None:
            self.put_text_hook(path, text)
        uid = path.removesuffix(".metadata")
        self.metadata[uid] = text
        self.metadata_mtime[uid] += 1.0
        self.text_writes.append((path, text))

    def put_file(self, local: Path, remote: str, callback=None) -> None:
        payload = local.read_bytes()
        self.payloads[remote] = payload
        self.file_writes.append((remote, payload))

    def stat_mtime(self, path: str) -> float:
        return self.metadata_mtime[path.removesuffix(".metadata")]

    def annotation_dir_mtime(self, uid: str) -> float:
        return 0.0

    def restart_xochitl(self) -> None:
        self.restart_count += 1


@pytest.fixture
def isolated_tags(tmp_path, monkeypatch) -> Path:
    config_directory = tmp_path / "config"
    tags_path = config_directory / "tags.json"
    monkeypatch.setattr(tags_module, "CONFIG_DIR", config_directory)
    monkeypatch.setattr(tags_module, "TAGS_PATH", tags_path)
    monkeypatch.setattr("esbern.library_metadata.TAGS_PATH", tags_path)
    return tags_path


def _library(tmp_path: Path, *, file_type: str = "epub") -> tuple[Path, Path]:
    root = tmp_path / "Library"
    root.mkdir()
    old_relpath = OLD_RELPATH if file_type == "epub" else "Messy Book.pdf"
    source = root / old_relpath
    source.write_bytes(b"original")
    stat = source.stat()
    State(
        root_uuid="root",
        files={
            old_relpath: FileEntry(
                uuid="book",
                file_type=file_type,
                size=stat.st_size,
                mtime=stat.st_mtime,
                tags=["Computing"],
            )
        },
    ).save(root)
    return root, source


def _save_tags(root: Path, *relpaths: str) -> None:
    store = TagStore()
    store.scope_to(root)
    for relpath in relpaths:
        store.set(relpath, ["Computing"])
    store.save()


def _fake_epub_write(path: Path, metadata: BookMetadata) -> None:
    path.write_bytes(path.read_bytes() + b"|normalized:" + metadata.title.encode())


def test_apply_preserves_uuid_and_updates_payload_state_and_scoped_tags(
    tmp_path, monkeypatch, isolated_tags
) -> None:
    root, source = _library(tmp_path)
    _save_tags(root, OLD_RELPATH)
    rm = FakeRemarkable()
    monkeypatch.setattr("esbern.library_metadata.write_epub_metadata", _fake_epub_write)

    result = apply_library_metadata(
        rm,
        root,
        [LibraryMetadataPlan(OLD_RELPATH, NEW_RELPATH, BOOK)],
    )

    destination = root / NEW_RELPATH
    assert not source.exists()
    assert destination.read_bytes() == b"original|normalized:Safe Systems"
    assert rm.payloads["book.epub"] == destination.read_bytes()
    assert xochitl.parse_metadata(rm.metadata["book"])["name"] == destination.stem
    saved = State.load(root)
    assert OLD_RELPATH not in saved.files
    assert saved.files[NEW_RELPATH].uuid == "book"
    store = TagStore.load()
    store.scope_to(root)
    assert store.get(OLD_RELPATH) == []
    assert store.get(NEW_RELPATH) == ["Computing"]
    assert (result.backup_directory / "files" / OLD_RELPATH).read_bytes() == b"original"
    assert result.files_updated == 1
    assert result.files_renamed == 1
    assert rm.restart_count == 0


def test_preflight_rejects_scoped_destination_tag_collision_before_writes(
    tmp_path, isolated_tags
) -> None:
    root, source = _library(tmp_path)
    _save_tags(root, OLD_RELPATH, NEW_RELPATH)
    rm = FakeRemarkable()

    with pytest.raises(BookMetadataError, match="destination tag record"):
        apply_library_metadata(
            rm,
            root,
            [LibraryMetadataPlan(OLD_RELPATH, NEW_RELPATH, BOOK)],
        )

    assert source.read_bytes() == b"original"
    assert rm.text_writes == []
    assert rm.file_writes == []
    assert rm.restart_count == 0
    assert not (root / ".esbern" / "metadata-backups").exists()


def test_late_destination_collision_does_not_clobber_and_rolls_back_device(
    tmp_path, isolated_tags
) -> None:
    root, source = _library(tmp_path, file_type="pdf")
    old_relpath = source.name
    new_relpath = NEW_RELPATH.removesuffix(".epub") + ".pdf"
    destination = root / new_relpath
    _save_tags(root, old_relpath)
    rm = FakeRemarkable(file_type="pdf")
    created = False

    def create_late_destination(path: str, text: str) -> None:
        nonlocal created
        if not created:
            created = True
            destination.write_bytes(b"concurrent")

    rm.put_text_hook = create_late_destination

    with pytest.raises(BookMetadataError, match="destination appeared"):
        apply_library_metadata(
            rm,
            root,
            [LibraryMetadataPlan(old_relpath, new_relpath, BOOK)],
        )

    assert source.read_bytes() == b"original"
    assert destination.read_bytes() == b"concurrent"
    assert xochitl.parse_metadata(rm.metadata["book"])["name"] == "Messy Book"
    assert list(State.load(root).files) == [old_relpath]
    assert rm.restart_count == 1


def test_checkpoint_failure_rolls_back_local_remote_state_and_tags(
    tmp_path, monkeypatch, isolated_tags
) -> None:
    root, source = _library(tmp_path)
    _save_tags(root, OLD_RELPATH)
    rm = FakeRemarkable()
    monkeypatch.setattr("esbern.library_metadata.write_epub_metadata", _fake_epub_write)
    original_save = TagStore.save
    calls = 0

    def fail_once(store: TagStore) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("checkpoint failed")
        original_save(store)

    monkeypatch.setattr(TagStore, "save", fail_once)

    with pytest.raises(BookMetadataError, match="checkpoint failed"):
        apply_library_metadata(
            rm,
            root,
            [LibraryMetadataPlan(OLD_RELPATH, NEW_RELPATH, BOOK)],
        )

    assert source.read_bytes() == b"original"
    assert not (root / NEW_RELPATH).exists()
    assert rm.payloads["book.epub"] == b"original"
    assert xochitl.parse_metadata(rm.metadata["book"])["name"] == "Messy Book"
    saved = State.load(root)
    assert list(saved.files) == [OLD_RELPATH]
    assert saved.files[OLD_RELPATH].uuid == "book"
    store = TagStore.load()
    store.scope_to(root)
    assert store.get(OLD_RELPATH) == ["Computing"]
    assert store.get(NEW_RELPATH) == []
    assert rm.restart_count == 1


def test_preflight_rejects_symlinked_source(tmp_path, isolated_tags) -> None:
    root = tmp_path / "Library"
    root.mkdir()
    outside = tmp_path / "outside.epub"
    outside.write_bytes(b"private")
    source = root / OLD_RELPATH
    source.symlink_to(outside)
    State(
        root_uuid="root",
        files={OLD_RELPATH: FileEntry(uuid="book", file_type="epub")},
    ).save(root)
    rm = FakeRemarkable()

    with pytest.raises(BookMetadataError, match="contains a symlink"):
        apply_library_metadata(
            rm,
            root,
            [LibraryMetadataPlan(OLD_RELPATH, NEW_RELPATH, BOOK)],
        )

    assert outside.read_bytes() == b"private"
    assert rm.text_writes == []
    assert rm.file_writes == []
