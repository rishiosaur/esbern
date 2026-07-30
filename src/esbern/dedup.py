"""Content-based duplicate detection for local book folders."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from esbern.state import State

BOOK_EXTENSIONS = {".epub", ".pdf"}
_COPY_SUFFIX = re.compile(r" \(\d+\)$")
ScanCallback = Callable[[Path, int, int], None]


@dataclass(frozen=True)
class DuplicateGroup:
    keeper: Path
    duplicates: tuple[Path, ...]
    size: int


@dataclass(frozen=True)
class DedupResult:
    groups: tuple[DuplicateGroup, ...]
    files_removed: int
    bytes_removed: int
    trash_directory: Path | None = None


class DeduplicationChangedError(RuntimeError):
    """Raised when a file changes between duplicate scan and removal."""


def _book_files(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in BOOK_EXTENSIONS
            and ".esbern" not in path.relative_to(root).parts
        ),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicate_groups(
    root: Path,
    scan_callback: ScanCallback | None = None,
    tracked_relpaths: set[str] | None = None,
) -> tuple[DuplicateGroup, ...]:
    """Find byte-identical PDF/EPUB groups and select one safe keeper."""
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in _book_files(root):
        by_size[path.stat().st_size].append(path)

    candidates = [
        path for paths in by_size.values() if len(paths) > 1 for path in paths
    ]
    by_content: dict[tuple[int, str], list[Path]] = defaultdict(list)
    for index, path in enumerate(candidates, start=1):
        if scan_callback:
            scan_callback(path, index, len(candidates))
        by_content[(path.stat().st_size, file_sha256(path))].append(path)

    tracked = (
        set(State.load(root).files) if tracked_relpaths is None else tracked_relpaths
    )

    def keeper_key(path: Path) -> tuple[bool, bool, int, str]:
        relpath = path.relative_to(root).as_posix()
        has_copy_suffix = bool(_COPY_SUFFIX.search(path.stem))
        return (
            relpath not in tracked,
            has_copy_suffix,
            len(relpath),
            relpath.casefold(),
        )

    groups: list[DuplicateGroup] = []
    for (size, _), paths in by_content.items():
        if len(paths) < 2:
            continue
        ordered = sorted(paths, key=keeper_key)
        groups.append(
            DuplicateGroup(keeper=ordered[0], duplicates=tuple(ordered[1:]), size=size)
        )
    return tuple(
        sorted(
            groups,
            key=lambda group: group.keeper.relative_to(root).as_posix().casefold(),
        )
    )


def _new_trash_directory(root: Path, category: str = "dedup-trash") -> Path:
    parent = root / ".esbern" / category
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    candidate = parent / timestamp
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{timestamp}-{suffix}"
        suffix += 1
    return candidate


def move_to_recovery(
    root: Path, files: list[Path] | tuple[Path, ...], *, category: str
) -> Path | None:
    """Move files out of the sync tree while keeping them recoverable."""
    if not files:
        return None
    recovery_directory = _new_trash_directory(root, category)
    for path in files:
        destination = recovery_directory / path.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.replace(destination)
    return recovery_directory


def deduplicate(
    root: Path,
    groups: tuple[DuplicateGroup, ...],
    *,
    dry_run: bool = False,
    permanent: bool = False,
) -> DedupResult:
    duplicates = [path for group in groups for path in group.duplicates]
    if not dry_run:
        for group in groups:
            try:
                keeper_digest = file_sha256(group.keeper)
                unchanged = group.keeper.stat().st_size == group.size
                unchanged = unchanged and all(
                    path.stat().st_size == group.size
                    and file_sha256(path) == keeper_digest
                    for path in group.duplicates
                )
            except FileNotFoundError as error:
                raise DeduplicationChangedError(
                    "a duplicate disappeared after the scan; rerun esbern dedup"
                ) from error
            if not unchanged:
                raise DeduplicationChangedError(
                    "a duplicate changed after the scan; rerun esbern dedup"
                )

    bytes_removed = sum(path.stat().st_size for path in duplicates)
    trash_directory = None

    if not dry_run and duplicates:
        if permanent:
            for path in duplicates:
                path.unlink()
        else:
            trash_directory = move_to_recovery(root, duplicates, category="dedup-trash")

    return DedupResult(
        groups=groups,
        files_removed=len(duplicates),
        bytes_removed=bytes_removed,
        trash_directory=trash_directory,
    )
