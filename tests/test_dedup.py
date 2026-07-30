from __future__ import annotations

import pytest

from esbern.dedup import (
    DeduplicationChangedError,
    deduplicate,
    find_duplicate_groups,
)
from esbern.state import FileEntry, State


def test_finds_exact_duplicates_and_prefers_unsuffixed_name(tmp_path) -> None:
    original = tmp_path / "Book.epub"
    copy_two = tmp_path / "Book (2).epub"
    copy_three = tmp_path / "Book (3).epub"
    distinct = tmp_path / "Different.epub"
    original.write_bytes(b"same book")
    copy_two.write_bytes(b"same book")
    copy_three.write_bytes(b"same book")
    distinct.write_bytes(b"different edition")

    groups = find_duplicate_groups(tmp_path)

    assert len(groups) == 1
    assert groups[0].keeper == original
    assert groups[0].duplicates == (copy_two, copy_three)


def test_prefers_tracked_file_as_keeper(tmp_path) -> None:
    original = tmp_path / "Book.epub"
    tracked_copy = tmp_path / "Book (2).epub"
    original.write_bytes(b"same")
    tracked_copy.write_bytes(b"same")
    State(files={tracked_copy.name: FileEntry(uuid="tracked", file_type="epub")}).save(
        tmp_path
    )

    groups = find_duplicate_groups(tmp_path)

    assert groups[0].keeper == tracked_copy
    assert groups[0].duplicates == (original,)


def test_deduplicate_moves_copies_to_recoverable_trash(tmp_path) -> None:
    original = tmp_path / "Book.pdf"
    duplicate = tmp_path / "Book (2).pdf"
    original.write_bytes(b"%PDF-same")
    duplicate.write_bytes(b"%PDF-same")
    groups = find_duplicate_groups(tmp_path)

    result = deduplicate(tmp_path, groups)

    assert original.exists()
    assert not duplicate.exists()
    assert result.files_removed == 1
    assert result.trash_directory is not None
    assert (result.trash_directory / duplicate.name).read_bytes() == b"%PDF-same"


def test_dry_run_does_not_move_files(tmp_path) -> None:
    original = tmp_path / "Book.epub"
    duplicate = tmp_path / "Book (2).epub"
    original.write_bytes(b"same")
    duplicate.write_bytes(b"same")

    result = deduplicate(tmp_path, find_duplicate_groups(tmp_path), dry_run=True)

    assert original.exists()
    assert duplicate.exists()
    assert result.files_removed == 1
    assert result.trash_directory is None


def test_deduplicate_stops_if_a_file_changed_after_the_scan(tmp_path) -> None:
    original = tmp_path / "Book.epub"
    duplicate = tmp_path / "Book (2).epub"
    original.write_bytes(b"same")
    duplicate.write_bytes(b"same")
    groups = find_duplicate_groups(tmp_path)
    duplicate.write_bytes(b"new edition")

    with pytest.raises(DeduplicationChangedError, match="changed after the scan"):
        deduplicate(tmp_path, groups, permanent=True)

    assert original.read_bytes() == b"same"
    assert duplicate.read_bytes() == b"new edition"
