"""Safe metadata enrichment and UUID-preserving library renames."""

from __future__ import annotations

import copy
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from esbern import xochitl
from esbern.book_metadata import (
    BookMetadata,
    BookMetadataError,
    canonical_filename,
    epub_isbns,
    epub_search_hints,
    lookup_google_books,
    write_epub_metadata,
)
from esbern.remarkable import Remarkable
from esbern.state import STATE_DIRNAME, STATE_FILENAME, FileEntry, State
from esbern.sync import SUPPORTED_EXTS
from esbern.tags import TAGS_PATH, TagStore

LibraryReporter = Callable[[str, str], None]


@dataclass(frozen=True)
class LibraryMetadataPlan:
    old_relpath: str
    new_relpath: str
    metadata: BookMetadata


@dataclass(frozen=True)
class LibraryMetadataFailure:
    relpath: str
    reason: str


@dataclass(frozen=True)
class LibraryMetadataResult:
    files_updated: int
    files_renamed: int
    backup_directory: Path


@dataclass(frozen=True)
class _PreparedPlan:
    plan: LibraryMetadataPlan
    old_path: Path
    new_path: Path


def _emit(reporter: LibraryReporter | None, action: str, detail: str) -> None:
    if reporter:
        reporter(action, detail)


def _search_query(path: Path) -> str:
    stem = " ".join(path.stem.split())
    if path.suffix.lower() == ".epub" and " - " not in stem:
        title, authors = epub_search_hints(path)
        if title and authors:
            return f"{title} {' '.join(authors)}"
    return stem


def plan_library_metadata(
    local_root: Path,
    *,
    api_key: str,
    reporter: LibraryReporter | None = None,
) -> tuple[list[LibraryMetadataPlan], list[LibraryMetadataFailure]]:
    """Resolve every tracked local book without changing disk or device state."""
    local_root = _absolute_root(local_root)
    state = State.load(local_root)
    plans: list[LibraryMetadataPlan] = []
    failures: list[LibraryMetadataFailure] = []
    old_paths = {relpath.casefold() for relpath in state.files}
    claimed: dict[str, str] = {}

    for index, relpath in enumerate(sorted(state.files, key=str.casefold), start=1):
        _emit(reporter, "lookup", f"[{index}/{len(state.files)}] {relpath}")
        try:
            path = _safe_local_path(local_root, relpath, role="source")
        except BookMetadataError as error:
            failures.append(LibraryMetadataFailure(relpath, str(error)))
            continue
        if not path.is_file():
            failures.append(LibraryMetadataFailure(relpath, "local file is missing"))
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            failures.append(LibraryMetadataFailure(relpath, "unsupported file type"))
            continue
        try:
            isbns = epub_isbns(path) if path.suffix.lower() == ".epub" else ()
            metadata = lookup_google_books(
                _search_query(path), isbns=isbns, api_key=api_key
            )
        except BookMetadataError as error:
            failures.append(LibraryMetadataFailure(relpath, str(error)))
            continue
        new_name = canonical_filename(metadata, path.suffix)
        new_relpath = (Path(relpath).parent / new_name).as_posix()
        try:
            destination = _safe_local_path(local_root, new_relpath, role="destination")
        except BookMetadataError as error:
            failures.append(LibraryMetadataFailure(relpath, str(error)))
            continue
        folded = new_relpath.casefold()
        owner = claimed.get(folded)
        if owner and owner != relpath:
            failures.append(
                LibraryMetadataFailure(relpath, f"canonical name collides with {owner}")
            )
            continue
        if folded in old_paths and folded != relpath.casefold():
            failures.append(
                LibraryMetadataFailure(
                    relpath, "canonical name is occupied by another tracked file"
                )
            )
            continue
        if destination.exists() and destination != path:
            failures.append(
                LibraryMetadataFailure(relpath, "canonical filename already exists")
            )
            continue
        claimed[folded] = relpath
        plans.append(LibraryMetadataPlan(relpath, new_relpath, metadata))
        _emit(reporter, "planned", f"{relpath} -> {new_relpath}")
    return plans, failures


def _absolute_root(local_root: Path) -> Path:
    root = local_root.expanduser().absolute()
    if root.is_symlink():
        raise BookMetadataError(f"library root must not be a symlink: {root}")
    if not root.is_dir():
        raise BookMetadataError(f"library root is not a directory: {root}")
    return root


def _safe_local_path(root: Path, relpath: str, *, role: str) -> Path:
    if not isinstance(relpath, str) or not relpath:
        raise BookMetadataError(f"invalid {role} path: {relpath!r}")
    relative = Path(relpath)
    components = relpath.replace("\\", "/").split("/")
    if (
        relative.is_absolute()
        or bool(PureWindowsPath(relpath).drive)
        or "\\" in relpath
        or "\x00" in relpath
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise BookMetadataError(f"unsafe {role} path: {relpath!r}")

    candidate = root
    for component in relative.parts:
        candidate /= component
        if candidate.is_symlink():
            raise BookMetadataError(f"{role} path contains a symlink: {relpath}")

    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise BookMetadataError(
            f"{role} path escapes the library root: {relpath}"
        ) from error
    return candidate


def _preflight_local_and_tags(
    local_root: Path,
    state: State,
    store: TagStore,
    plans: list[LibraryMetadataPlan],
) -> list[_PreparedPlan]:
    """Validate the complete local/tag plan before creating backups or writing."""
    state_paths = {relpath.casefold(): relpath for relpath in state.files}
    claimed_sources: set[str] = set()
    claimed_destinations: dict[str, str] = {}
    prepared: list[_PreparedPlan] = []

    for plan in plans:
        if plan.old_relpath not in state.files:
            raise BookMetadataError(
                f"{plan.old_relpath}: tracked state entry is missing"
            )
        if plan.old_relpath in claimed_sources:
            raise BookMetadataError(
                f"{plan.old_relpath}: metadata plan contains the source twice"
            )
        claimed_sources.add(plan.old_relpath)

        old_path = _safe_local_path(local_root, plan.old_relpath, role="source")
        new_path = _safe_local_path(local_root, plan.new_relpath, role="destination")
        if old_path.parent != new_path.parent:
            raise BookMetadataError(
                f"{plan.old_relpath}: normalization may not move books between folders"
            )
        if not old_path.is_file():
            raise BookMetadataError(f"{plan.old_relpath}: local file is missing")
        if not new_path.parent.is_dir():
            raise BookMetadataError(
                f"{plan.new_relpath}: destination folder is missing"
            )

        entry = state.files[plan.old_relpath]
        expected_suffix = f".{entry.file_type.lower()}"
        if (
            old_path.suffix.lower() != expected_suffix
            or new_path.suffix.lower() != expected_suffix
            or old_path.suffix.lower() not in SUPPORTED_EXTS
        ):
            raise BookMetadataError(
                f"{plan.old_relpath}: plan and tracked file type do not match"
            )

        destination_key = plan.new_relpath.casefold()
        owner = claimed_destinations.get(destination_key)
        if owner is not None and owner != plan.old_relpath:
            raise BookMetadataError(
                f"{plan.new_relpath}: destination collides with {owner}"
            )
        claimed_destinations[destination_key] = plan.old_relpath

        occupied = state_paths.get(destination_key)
        if occupied is not None and occupied.casefold() != plan.old_relpath.casefold():
            raise BookMetadataError(
                f"{plan.new_relpath}: destination is tracked as {occupied}"
            )
        if new_path != old_path and (new_path.exists() or new_path.is_symlink()):
            raise BookMetadataError(f"{plan.new_relpath}: destination already exists")
        if new_path != old_path and store.entry(plan.new_relpath) is not None:
            raise BookMetadataError(
                f"{plan.new_relpath}: destination tag record already exists"
            )

        prepared.append(_PreparedPlan(plan, old_path, new_path))
    return prepared


def _rename_without_clobber(source: Path, destination: Path) -> None:
    """Atomically claim a new filename without replacing an existing entry."""
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError as error:
        raise BookMetadataError(
            f"destination appeared while applying metadata: {destination.name}"
        ) from error
    except OSError as error:
        raise BookMetadataError(
            f"could not reserve metadata destination {destination.name}: {error}"
        ) from error
    try:
        source.unlink()
    except OSError as error:
        try:
            if source.exists() and destination.samefile(source):
                destination.unlink()
        except OSError:
            pass
        raise BookMetadataError(
            f"could not finish metadata rename to {destination.name}: {error}"
        ) from error


def _backup_directory(local_root: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = local_root / STATE_DIRNAME / "metadata-backups" / timestamp
    candidate = base
    number = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{number}")
        number += 1
    candidate.mkdir(parents=True)
    return candidate


def _validate_remote(
    rm: Remarkable,
    state: State,
    plans: list[LibraryMetadataPlan],
) -> dict[str, str]:
    records = rm.read_xochitl_metadata()
    metadata_by_uuid = {uid: raw for uid, _, raw in records}
    parsed_by_uuid: dict[str, dict] = {}
    for uid, raw in metadata_by_uuid.items():
        try:
            parsed_by_uuid[uid] = xochitl.parse_metadata(raw)
        except (TypeError, ValueError) as error:
            raise BookMetadataError(
                f"reMarkable metadata for {uid} is invalid"
            ) from error
    root = parsed_by_uuid.get(state.root_uuid)
    if root is None:
        raise BookMetadataError("saved reMarkable root UUID is missing")
    if root["type"] != "CollectionType" or root["parent"] or root["deleted"]:
        raise BookMetadataError("saved reMarkable root is not an active collection")

    def is_active_descendant(uid: str) -> bool:
        seen: set[str] = set()
        current = uid
        while current and current not in seen:
            seen.add(current)
            node = parsed_by_uuid.get(current)
            if node is None or node["deleted"] or node["parent"] == "trash":
                return False
            if node["parent"] == state.root_uuid:
                return True
            current = node["parent"]
        return False

    originals: dict[str, str] = {}
    for plan in plans:
        entry = state.files[plan.old_relpath]
        node = parsed_by_uuid.get(entry.uuid)
        if node is None or not is_active_descendant(entry.uuid):
            raise BookMetadataError(
                f"{plan.old_relpath}: UUID is not active under the saved root"
            )
        if node["type"] != "DocumentType" or node["deleted"]:
            raise BookMetadataError(
                f"{plan.old_relpath}: remote UUID is not an active document"
            )
        raw = metadata_by_uuid.get(entry.uuid)
        if raw is None:
            raise BookMetadataError(f"{plan.old_relpath}: remote metadata is missing")
        payload = rm.remote_path(f"{entry.uuid}.{entry.file_type}")
        if not rm.exists(payload):
            raise BookMetadataError(
                f"{plan.old_relpath}: remote {entry.file_type} payload is missing"
            )
        originals[entry.uuid] = raw
    return originals


def _rollback_item(
    rm: Remarkable,
    local_root: Path,
    prepared: _PreparedPlan,
    entry: FileEntry,
    local_original: Path,
    state_before: State,
    store_before: TagStore,
    *,
    local_payload_mutated: bool,
    local_rename_attempted: bool,
    local_renamed: bool,
    remote_payload_attempted: bool,
    remote_metadata_attempted: bool,
    remote_metadata_before: str,
    device_mutation_attempted: bool,
) -> list[str]:
    """Best-effort restore one item and return descriptions of rollback errors."""
    errors: list[str] = []
    old_path = prepared.old_path
    new_path = prepared.new_path

    if remote_payload_attempted:
        try:
            rm.put_file(
                local_original,
                rm.remote_path(f"{entry.uuid}.{entry.file_type}"),
            )
        except Exception as error:  # noqa: BLE001 - collect every rollback failure
            errors.append(f"remote payload restore failed: {error}")

    if remote_metadata_attempted:
        try:
            rm.put_text(
                rm.remote_path(f"{entry.uuid}.metadata"),
                remote_metadata_before,
            )
        except Exception as error:  # noqa: BLE001 - collect every rollback failure
            errors.append(f"remote name restore failed: {error}")

    try:
        if local_renamed:
            if new_path.is_symlink() or not new_path.is_file():
                raise OSError(f"renamed payload is missing or unsafe: {new_path}")
            if old_path.exists() or old_path.is_symlink():
                raise OSError(
                    f"original filename was concurrently occupied: {old_path}"
                )
            shutil.copy2(local_original, new_path)
            _rename_without_clobber(new_path, old_path)
        else:
            if (
                local_rename_attempted
                and new_path.exists()
                and old_path.exists()
                and new_path.samefile(old_path)
            ):
                new_path.unlink()
            if local_payload_mutated:
                if old_path.is_symlink() or not old_path.is_file():
                    raise OSError(f"original payload is missing or unsafe: {old_path}")
                shutil.copy2(local_original, old_path)
    except Exception as error:  # noqa: BLE001 - collect every rollback failure
        errors.append(f"local payload restore failed: {error}")

    try:
        state_before.save(local_root)
    except Exception as error:  # noqa: BLE001 - collect every rollback failure
        errors.append(f"state restore failed: {error}")
    try:
        store_before.save()
    except Exception as error:  # noqa: BLE001 - collect every rollback failure
        errors.append(f"tag restore failed: {error}")

    if device_mutation_attempted:
        try:
            rm.restart_xochitl()
        except Exception as error:  # noqa: BLE001 - collect every rollback failure
            errors.append(f"xochitl refresh failed: {error}")
    return errors


def _metadata_error(relpath: str, error: BaseException, rollback: list[str]) -> str:
    detail = str(error) or type(error).__name__
    message = f"{relpath}: metadata update failed: {detail}"
    if rollback:
        message += "; rollback incomplete: " + "; ".join(rollback)
    return message


def apply_library_metadata(
    rm: Remarkable,
    local_root: Path,
    plans: list[LibraryMetadataPlan],
    *,
    backup_files: bool = True,
    reporter: LibraryReporter | None = None,
) -> LibraryMetadataResult:
    """Apply a fully resolved plan, checkpointing each preserved UUID."""
    local_root = _absolute_root(local_root)
    state = State.load(local_root)
    store = TagStore.load()
    store.scope_to(local_root)
    prepared_plans = _preflight_local_and_tags(local_root, state, store, plans)
    originals = _validate_remote(rm, state, plans)

    if backup_files:
        required = sum(item.old_path.stat().st_size for item in prepared_plans)
        available = shutil.disk_usage(local_root).free
        if available < required + 100 * 1024 * 1024:
            raise BookMetadataError(
                "not enough free disk space for the requested metadata backup"
            )

    backup = _backup_directory(local_root)
    state_path = local_root / STATE_DIRNAME / STATE_FILENAME
    if state_path.exists():
        shutil.copy2(state_path, backup / STATE_FILENAME)
    if TAGS_PATH.exists():
        shutil.copy2(TAGS_PATH, backup / "tags.json")
    (backup / "remote-metadata.json").write_text(
        json.dumps(originals, indent=2), encoding="utf-8"
    )
    (backup / "plan.json").write_text(
        json.dumps(
            [
                {
                    "old_relpath": plan.old_relpath,
                    "new_relpath": plan.new_relpath,
                    "google_id": plan.metadata.google_id,
                }
                for plan in plans
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    renamed = 0
    rollback_directory = backup / ".rollback"
    for index, prepared in enumerate(prepared_plans, start=1):
        plan = prepared.plan
        old_path = prepared.old_path
        new_path = prepared.new_path
        entry: FileEntry = state.files[plan.old_relpath]
        _emit(
            reporter,
            "updating",
            f"[{index}/{len(prepared_plans)}] {plan.old_relpath}",
        )

        state_before = copy.deepcopy(state)
        store_before = copy.deepcopy(store)
        local_payload_mutated = False
        local_rename_attempted = False
        local_renamed = False
        remote_payload_attempted = False
        remote_metadata_attempted = False
        device_mutation_attempted = False
        state_or_tags_mutated = False
        remote_metadata_before = originals[entry.uuid]

        if backup_files:
            local_original = backup / "files" / plan.old_relpath
        else:
            rollback_directory.mkdir(parents=True, exist_ok=True)
            local_original = rollback_directory / f"{index}{old_path.suffix}"

        try:
            local_original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, local_original)

            if entry.file_type == "epub":
                local_payload_mutated = True
                write_epub_metadata(old_path, plan.metadata)
                remote_payload_attempted = True
                device_mutation_attempted = True
                rm.put_file(old_path, rm.remote_path(f"{entry.uuid}.epub"))

            metadata_path = rm.remote_path(f"{entry.uuid}.metadata")
            remote_metadata_before = rm.get_text(metadata_path)
            parsed = xochitl.parse_metadata(remote_metadata_before)
            desired_name = Path(plan.new_relpath).stem
            if parsed["name"] != desired_name:
                remote_metadata_attempted = True
                device_mutation_attempted = True
                rm.put_text(
                    metadata_path,
                    xochitl.update_name(remote_metadata_before, desired_name),
                )

            if new_path != old_path:
                local_rename_attempted = True
                _rename_without_clobber(old_path, new_path)
                local_renamed = True

            stat = new_path.stat()
            entry.size = stat.st_size
            entry.mtime = stat.st_mtime
            entry.remote_mtime = max(
                rm.stat_mtime(metadata_path), rm.annotation_dir_mtime(entry.uuid)
            )
            state_or_tags_mutated = True
            state.files.pop(plan.old_relpath)
            state.files[plan.new_relpath] = entry
            store.move(plan.old_relpath, plan.new_relpath)
            state.save(local_root)
            store.save()
        except BaseException as error:
            mutation_started = any(
                (
                    local_payload_mutated,
                    local_rename_attempted,
                    remote_payload_attempted,
                    remote_metadata_attempted,
                    state_or_tags_mutated,
                )
            )
            rollback_errors: list[str] = []
            if mutation_started:
                rollback_errors = _rollback_item(
                    rm,
                    local_root,
                    prepared,
                    entry,
                    local_original,
                    state_before,
                    store_before,
                    local_payload_mutated=local_payload_mutated,
                    local_rename_attempted=local_rename_attempted,
                    local_renamed=local_renamed,
                    remote_payload_attempted=remote_payload_attempted,
                    remote_metadata_attempted=remote_metadata_attempted,
                    remote_metadata_before=remote_metadata_before,
                    device_mutation_attempted=device_mutation_attempted,
                )
                state = state_before
                store = store_before
            if not isinstance(error, Exception):
                raise
            raise BookMetadataError(
                _metadata_error(plan.old_relpath, error, rollback_errors)
            ) from error

        if not backup_files:
            try:
                local_original.unlink(missing_ok=True)
            except OSError as error:
                _emit(
                    reporter,
                    "warning",
                    f"temporary rollback copy retained: {error}",
                )
        renamed += int(local_renamed)
        _emit(reporter, "updated", plan.new_relpath)

    if rollback_directory.exists():
        try:
            rollback_directory.rmdir()
        except OSError:
            pass

    return LibraryMetadataResult(
        files_updated=len(prepared_plans),
        files_renamed=renamed,
        backup_directory=backup,
    )
