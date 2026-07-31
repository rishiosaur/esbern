from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from threading import Barrier

import pytest

from esbern import tags as tags_module
from esbern import xochitl
from esbern.book_metadata import BookMetadata, BookMetadataError
from esbern.library_metadata import (
    LibraryMetadataPlan,
    _filename_hints,
    _metadata_from_override,
    _rename_without_clobber,
    _verified_authors,
    apply_library_metadata,
    load_library_metadata_plan,
    plan_library_metadata,
    resume_pending_library_metadata,
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
        self.put_file_hook: Callable[[Path, str], None] | None = None
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
        if self.put_file_hook is not None:
            self.put_file_hook(local, remote)
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


def test_filename_hints_remove_series_publisher_and_repeated_author() -> None:
    path = Path(
        "[Series 1] Vinge, Vernor - A Deepness in the Sky - Vernor Vinge "
        "(2010, Publisher).epub"
    )

    title, authors, strict = _filename_hints(path)

    assert title == "A Deepness in the Sky"
    assert authors == ("Vinge, Vernor",)
    assert strict is True


def test_filename_hints_remove_publisher_parenthetical_and_inline_author() -> None:
    path = Path("Egan, Greg - Luminous_ Greg Egan (2013, Greg Egan).epub")

    title, authors, strict = _filename_hints(path)

    assert title == "Luminous"
    assert authors == ("Egan, Greg",)
    assert strict is True


def test_verified_authors_removes_unrelated_catalog_contributor() -> None:
    metadata = BookMetadata(
        google_id="id",
        title="Distress",
        authors=("Greg Egan", "MONDADORI"),
        published_date="2013",
    )

    result = _verified_authors(metadata, ("Greg Egan",), True)

    assert result.authors == ("Greg Egan",)


def test_library_override_resolves_without_google(tmp_path, monkeypatch) -> None:
    root, source = _library(tmp_path, file_type="pdf")
    overrides = {
        source.name: {
            "title": "Safe Systems",
            "authors": ["Ada Lovelace"],
            "published_date": "2024",
        }
    }
    (root / ".esbern" / "metadata-overrides.json").write_text(
        json.dumps(overrides), encoding="utf-8"
    )

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("Google lookup should not run for an override")

    monkeypatch.setattr(
        "esbern.library_metadata.resolve_book_metadata", unexpected_lookup
    )

    plans, failures = plan_library_metadata(root, api_key="secret")

    assert failures == []
    assert plans[0].new_relpath == NEW_RELPATH.removesuffix(".epub") + ".pdf"


def test_library_plan_uses_local_fallback_when_google_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    root, _source = _library(tmp_path, file_type="pdf")
    fallback = BookMetadata(
        google_id="",
        title="Safe Systems",
        authors=("Ada Lovelace",),
        published_date="2024",
    )
    reports = []
    monkeypatch.setattr(
        "esbern.library_metadata.resolve_book_metadata",
        lambda *args, **kwargs: (fallback, "libgen"),
    )

    plans, failures = plan_library_metadata(
        root,
        api_key="exhausted",
        reporter=lambda action, detail: reports.append((action, detail)),
    )

    assert failures == []
    assert plans[0].new_relpath == NEW_RELPATH.removesuffix(".epub") + ".pdf"
    assert ("fallback", "Messy Book.pdf") in reports


def test_all_epubs_are_prepared_in_parallel_before_device_writes(
    tmp_path, monkeypatch, isolated_tags
) -> None:
    root, source = _library(tmp_path)
    second_relpath = "Another Messy Book.epub"
    second_path = root / second_relpath
    second_path.write_bytes(b"second")
    second_stat = second_path.stat()
    state = State.load(root)
    state.files[second_relpath] = FileEntry(
        uuid="book2",
        file_type="epub",
        size=second_stat.st_size,
        mtime=second_stat.st_mtime,
    )
    state.save(root)

    second_book = BookMetadata(
        google_id="second",
        title="Parallel Systems",
        authors=("Grace Hopper",),
        published_date="2023",
    )
    second_new = "Grace Hopper - Parallel Systems (2023).epub"
    rm = FakeRemarkable()
    rm.metadata["book2"] = xochitl.document_metadata(
        "Another Messy Book", parent="root"
    )
    rm.metadata_mtime["book2"] = 1.0
    rm.payloads["book2.epub"] = b"second"

    barrier = Barrier(2)
    prepared: set[str] = set()

    def parallel_epub_write(path: Path, metadata: BookMetadata) -> None:
        barrier.wait(timeout=2)
        prepared.add(metadata.title)
        _fake_epub_write(path, metadata)

    def require_complete_preparation(local: Path, remote: str) -> None:
        assert prepared == {"Safe Systems", "Parallel Systems"}
        assert not source.exists()
        assert not second_path.exists()
        assert (root / NEW_RELPATH).is_file()
        assert (root / second_new).is_file()

    monkeypatch.setattr(
        "esbern.library_metadata.write_epub_metadata", parallel_epub_write
    )
    rm.put_file_hook = require_complete_preparation

    result = apply_library_metadata(
        rm,
        root,
        [
            LibraryMetadataPlan(OLD_RELPATH, NEW_RELPATH, BOOK),
            LibraryMetadataPlan(second_relpath, second_new, second_book),
        ],
        workers=2,
    )

    assert result.files_updated == 2
    assert (root / NEW_RELPATH).is_file()
    assert (root / second_new).is_file()


def test_metadata_override_rejects_missing_year() -> None:
    with pytest.raises(BookMetadataError, match="at least one author, and a year"):
        _metadata_from_override(
            "book.pdf",
            {
                "title": "Safe Systems",
                "authors": ["Ada Lovelace"],
                "published_date": "unknown",
            },
        )


def test_loads_legacy_complete_plan_from_prepared_epub(
    tmp_path, monkeypatch, isolated_tags
) -> None:
    root, _ = _library(tmp_path)
    backup = root / ".esbern" / "metadata-backups" / "saved"
    prepared = backup / ".prepared"
    prepared.mkdir(parents=True)
    (prepared / "1.epub").write_bytes(b"prepared")
    (backup / "plan.json").write_text(
        json.dumps(
            [
                {
                    "old_relpath": OLD_RELPATH,
                    "new_relpath": NEW_RELPATH,
                    "google_id": BOOK.google_id,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "esbern.library_metadata.read_epub_metadata", lambda path: BOOK
    )

    plans = load_library_metadata_plan(root, backup)

    assert plans == [LibraryMetadataPlan(OLD_RELPATH, NEW_RELPATH, BOOK)]


def test_apply_preserves_uuid_and_updates_payload_state_and_scoped_tags(
    tmp_path, monkeypatch, isolated_tags
) -> None:
    root, source = _library(tmp_path)
    _save_tags(root, OLD_RELPATH)
    overrides_path = root / ".esbern" / "metadata-overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                OLD_RELPATH: {
                    "title": BOOK.title,
                    "authors": list(BOOK.authors),
                    "published_date": BOOK.published_date,
                }
            }
        ),
        encoding="utf-8",
    )
    rm = FakeRemarkable()
    rm.metadata["unrelated"] = "{invalid json"
    rm.metadata_mtime["unrelated"] = 1.0
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
    saved_overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
    assert OLD_RELPATH not in saved_overrides
    assert saved_overrides[NEW_RELPATH]["title"] == BOOK.title
    assert (result.backup_directory / "files" / OLD_RELPATH).read_bytes() == b"original"
    backed_up_overrides = json.loads(
        (result.backup_directory / "metadata-overrides.json").read_text(
            encoding="utf-8"
        )
    )
    assert OLD_RELPATH in backed_up_overrides
    saved_plan = json.loads((result.backup_directory / "plan.json").read_text())
    assert saved_plan[0]["metadata"]["title"] == BOOK.title
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


def test_late_destination_collision_rolls_back_local_batch_before_device_writes(
    tmp_path, monkeypatch, isolated_tags
) -> None:
    root, source = _library(tmp_path, file_type="pdf")
    old_relpath = source.name
    new_relpath = NEW_RELPATH.removesuffix(".epub") + ".pdf"
    destination = root / new_relpath
    _save_tags(root, old_relpath)
    rm = FakeRemarkable(file_type="pdf")
    original_rename = _rename_without_clobber

    def create_late_destination(source_path: Path, destination_path: Path) -> None:
        if destination_path == destination and not destination.exists():
            destination.write_bytes(b"concurrent")
        original_rename(source_path, destination_path)

    monkeypatch.setattr(
        "esbern.library_metadata._rename_without_clobber",
        create_late_destination,
    )

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
    assert rm.text_writes == []
    assert rm.file_writes == []
    assert rm.restart_count == 0
    assert not (root / ".esbern" / "metadata-overrides.json").exists()


def test_local_checkpoint_failure_rolls_back_before_device_writes(
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
    assert rm.text_writes == []
    assert rm.file_writes == []
    assert rm.restart_count == 0
    assert not (root / ".esbern" / "metadata-overrides.json").exists()


def test_remote_failure_keeps_canonical_local_batch_pending_and_rolls_back_device(
    tmp_path, monkeypatch, isolated_tags
) -> None:
    root, source = _library(tmp_path)
    _save_tags(root, OLD_RELPATH)
    rm = FakeRemarkable()
    monkeypatch.setattr("esbern.library_metadata.write_epub_metadata", _fake_epub_write)
    calls = 0

    def fail_first_name_write(path: str, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("device name failed")

    rm.put_text_hook = fail_first_name_write

    with pytest.raises(BookMetadataError, match="device name failed"):
        apply_library_metadata(
            rm,
            root,
            [LibraryMetadataPlan(OLD_RELPATH, NEW_RELPATH, BOOK)],
        )

    destination = root / NEW_RELPATH
    assert not source.exists()
    assert destination.read_bytes() == b"original|normalized:Safe Systems"
    assert rm.payloads["book.epub"] == b"original"
    assert xochitl.parse_metadata(rm.metadata["book"])["name"] == "Messy Book"
    saved = State.load(root)
    assert OLD_RELPATH not in saved.files
    assert saved.files[NEW_RELPATH].uuid == "book"
    assert saved.files[NEW_RELPATH].size == -1
    assert saved.files[NEW_RELPATH].mtime == 0.0
    store = TagStore.load()
    store.scope_to(root)
    assert store.get(OLD_RELPATH) == []
    assert store.get(NEW_RELPATH) == ["Computing"]
    assert rm.restart_count == 1


def test_resume_pending_updates_only_sentinel_entries(
    tmp_path, isolated_tags
) -> None:
    root, source = _library(tmp_path)
    destination = root / NEW_RELPATH
    source.rename(destination)
    state = State.load(root)
    entry = state.files.pop(OLD_RELPATH)
    entry.size = -1
    entry.mtime = 0.0
    state.files[NEW_RELPATH] = entry
    state.save(root)
    rm = FakeRemarkable()

    result = resume_pending_library_metadata(rm, root)

    assert result.files_updated == 1
    assert rm.payloads["book.epub"] == b"original"
    assert xochitl.parse_metadata(rm.metadata["book"])["name"] == destination.stem
    saved = State.load(root)
    assert saved.files[NEW_RELPATH].size == destination.stat().st_size
    assert saved.files[NEW_RELPATH].mtime == destination.stat().st_mtime


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
