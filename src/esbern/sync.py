"""Two-way sync between a local folder and a Collection on the reMarkable.

Hard rule: this code NEVER deletes a file or folder on either side. If
something disappears locally, we leave it on the device. If something
disappears on the device, we leave it locally. Anything in our state but
missing on both sides simply gets pruned from state.

Pull phase (device → local, always first):
  - List all xochitl items and materialize the device tree locally.
  - When an untracked local book has the same folder/title as a remote book,
    the remote copy wins. The local copy is moved to recoverable sync trash.

Dedup phase:
  - Hash byte-identical local PDF/EPUB candidates after pulling. Prefer a
    remote-backed file as keeper and move untracked duplicates to recovery.

Push phase (local → device, only after pull and dedup):
  - Walk local tree, ensure a child Collection per local subdirectory and
    a Document per local .pdf/.epub under our root Collection.
  - For new files, ask the LLM tagger for tags (using filename + first
    pages of extracted text). Write tags into xochitl .metadata and store
    them in the central tag store.
  - For files that look unchanged (same size/mtime) on a known UUID, skip.

Conflict policy: if both sides changed since last sync, prefer the
device (where annotations happen). Print a notice.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from threading import local as thread_local

import click

from esbern import xochitl
from esbern.dedup import (
    DuplicateGroup,
    deduplicate,
    file_sha256,
    find_duplicate_groups,
    move_to_recovery,
)
from esbern.remarkable import Remarkable
from esbern.state import FileEntry, State
from esbern.tags import TagStore, categorize, extract_pdf_text

SUPPORTED_EXTS = {".pdf", ".epub"}
IGNORE_NAMES = {".DS_Store", ".esbern"}

_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class SyncStats:
    folders_created: int = 0
    files_uploaded: int = 0
    files_updated: int = 0
    files_pulled: int = 0
    files_repulled: int = 0
    folders_pulled: int = 0
    tags_assigned: int = 0
    skipped: int = 0
    conflicts: int = 0
    duplicates_removed: int = 0
    duplicate_bytes_removed: int = 0
    files_linked: int = 0


@dataclass(frozen=True)
class SyncEvent:
    """A verbose sync event suitable for terminal or structured reporting."""

    kind: str  # phase | item | transfer
    phase: str  # setup | push | tags | scan | pull | finish
    action: str
    item: str = ""
    current: int | None = None
    total: int | None = None


SyncReporter = Callable[[SyncEvent], None]


@dataclass(frozen=True)
class _UploadPlan:
    """Everything a worker needs to classify and upload one local book."""

    rel: Path
    local_path: Path
    parent_uuid: str
    uid: str
    file_type: str
    display_name: str
    size: int
    mtime: float
    cached_tags: tuple[str, ...]
    updating: bool
    current: int
    total: int


def _emit(
    reporter: SyncReporter | None,
    kind: str,
    phase: str,
    action: str,
    item: str = "",
    current: int | None = None,
    total: int | None = None,
) -> None:
    if reporter:
        reporter(
            SyncEvent(
                kind=kind,
                phase=phase,
                action=action,
                item=item,
                current=current,
                total=total,
            )
        )


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _safe_name(name: str) -> str:
    cleaned = _BAD_NAME_CHARS.sub("_", name).strip().rstrip(".")
    return cleaned or "untitled"


def _walk_local(root: Path):
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name in IGNORE_NAMES:
            continue
        if entry.is_symlink():
            continue
        rel = entry.relative_to(root)
        if entry.is_dir():
            yield rel, True
            yield from ((rel / r, d) for r, d in _walk_local(entry))
        elif entry.is_file():
            yield rel, False


# ---------- push side ----------


def _metadata_records(rm: Remarkable) -> list[tuple[str, float, str]]:
    bulk_reader = getattr(rm, "read_xochitl_metadata", None)
    if callable(bulk_reader):
        return bulk_reader()
    records: list[tuple[str, float, str]] = []
    for uid, mtime in rm.list_xochitl_metadata():
        try:
            raw = rm.get_text(rm.remote_path(f"{uid}.metadata"))
        except OSError:
            continue
        records.append((uid, mtime, raw))
    return records


def _find_existing_root_collection(rm: Remarkable, name: str) -> str | None:
    """Find a unique, visible top-level Collection matching ``name``.

    Local filesystems and the reMarkable UI do not necessarily preserve the
    same capitalization, so fall back to a case-insensitive match when there
    is no exact one.  Ambiguous matches are an error: silently choosing one
    would merge the local tree into an arbitrary device folder.
    """
    candidates: list[tuple[str, str]] = []
    for uid, _, raw in _metadata_records(rm):
        try:
            parsed = xochitl.parse_metadata(raw)
        except (ValueError, TypeError, AttributeError):
            continue
        if (
            parsed["type"] == "CollectionType"
            and parsed["parent"] == ""
            and not parsed["deleted"]
            and parsed["name"].casefold() == name.casefold()
        ):
            candidates.append((uid, parsed["name"]))

    if not candidates:
        return None
    if len(candidates) > 1:
        details = ", ".join(
            f"'{visible_name}' ({uid})" for uid, visible_name in candidates
        )
        raise click.ClickException(
            f"More than one top-level reMarkable folder matches '{name}': "
            f"{details}. Rename or remove the duplicate folders on the "
            "device, then run sync again."
        )
    return candidates[0][0]


def _tag_file(
    local: Path, relpath: str, store: TagStore, stats: SyncStats
) -> list[str]:
    existing = store.get(relpath)
    if existing:
        return existing
    sample = extract_pdf_text(local) if local.suffix.lower() == ".pdf" else ""
    tags = categorize(local.name, sample, store.taxonomy)
    if tags:
        store.set(relpath, tags)
        stats.tags_assigned += 1
    return tags


def _ensure_root_collection(
    rm: Remarkable,
    state: State,
    name: str,
    stats: SyncStats,
    reporter: SyncReporter | None = None,
) -> str:
    if state.root_uuid:
        meta_path = rm.remote_path(f"{state.root_uuid}.metadata")
        if rm.exists(meta_path):
            raw = rm.get_text(meta_path)
            parsed = xochitl.parse_metadata(raw)
            valid_saved_root = (
                parsed["type"] == "CollectionType"
                and parsed["parent"] == ""
                and not parsed["deleted"]
                and parsed["name"].casefold() == name.casefold()
            )
            if not valid_saved_root:
                stale_root = state.root_uuid
                state.root_uuid = ""
                state.folders.clear()
                state.folder_remote_mtime.clear()
                state.files.clear()
                if parsed["parent"] == "trash" or parsed["deleted"]:
                    action = "trashed root ignored"
                    reason = parsed["name"] or name
                else:
                    action = "out-of-scope root ignored"
                    reason = parsed["name"] or "unnamed item"
                _emit(
                    reporter,
                    "item",
                    "setup",
                    action,
                    f"{reason} ({stale_root[:8]})",
                )
            else:
                _emit(
                    reporter,
                    "item",
                    "push",
                    "root linked",
                    f"{name} ({state.root_uuid[:8]})",
                )
            if state.root_uuid:
                return state.root_uuid

    if not state.root_uuid:
        existing = _find_existing_root_collection(rm, name)
        if existing:
            state.root_uuid = existing
            message = f"{name} ({existing[:8]})"
            if reporter:
                _emit(reporter, "item", "push", "root adopted", message)
            else:
                click.echo(f"  linked existing reMarkable folder: {message}")
            return existing

    uid = state.root_uuid or _new_uuid()
    rm.put_text(
        rm.remote_path(f"{uid}.metadata"),
        xochitl.collection_metadata(name=name, parent=""),
    )
    rm.put_text(rm.remote_path(f"{uid}.content"), xochitl.empty_collection_content())
    state.root_uuid = uid
    stats.folders_created += 1
    _emit(reporter, "item", "push", "root created", f"{name} ({uid[:8]})")
    return uid


def _ensure_subfolder(
    rm: Remarkable,
    state: State,
    rel: Path,
    parent_uuid: str,
    stats: SyncStats,
    reporter: SyncReporter | None = None,
    current: int | None = None,
    total: int | None = None,
) -> str:
    key = rel.as_posix()
    existing = state.folders.get(key)
    if existing:
        metadata_path = rm.remote_path(f"{existing}.metadata")
        if rm.exists(metadata_path):
            try:
                parsed = xochitl.parse_metadata(rm.get_text(metadata_path))
            except (OSError, ValueError, TypeError, AttributeError):
                parsed = None
            if (
                parsed
                and parsed["type"] == "CollectionType"
                and not parsed["deleted"]
                and parsed["parent"] == parent_uuid
                and parsed["name"].casefold() == rel.name.casefold()
            ):
                _emit(
                    reporter, "item", "push", "folder unchanged", key, current, total
                )
                return existing
            _emit(
                reporter,
                "item",
                "push",
                "stale folder mapping replaced",
                key,
                current,
                total,
            )
            existing = None
    uid = existing or _new_uuid()
    rm.put_text(
        rm.remote_path(f"{uid}.metadata"),
        xochitl.collection_metadata(name=rel.name, parent=parent_uuid),
    )
    rm.put_text(rm.remote_path(f"{uid}.content"), xochitl.empty_collection_content())
    state.folders[key] = uid
    stats.folders_created += 1
    _emit(reporter, "item", "push", "folder created", key, current, total)
    return uid


def _upload_file(
    rm: Remarkable,
    state: State,
    store: TagStore,
    local_root: Path,
    rel: Path,
    parent_uuid: str,
    stats: SyncStats,
    reporter: SyncReporter | None = None,
    current: int | None = None,
    total: int | None = None,
) -> None:
    ext = rel.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        stats.skipped += 1
        _emit(reporter, "item", "push", "unsupported", rel.as_posix(), current, total)
        return
    local_path = local_root / rel
    st = local_path.stat()
    key = rel.as_posix()
    prev = state.files.get(key)

    file_type = ext.lstrip(".")
    display_name = rel.stem

    # Detect whether each side changed since last sync.
    local_changed = not prev or prev.size != st.st_size or prev.mtime != st.st_mtime
    remote_changed = False
    remote_metadata_exists = False
    if prev:
        meta_path = rm.remote_path(f"{prev.uuid}.metadata")
        if rm.exists(meta_path):
            remote_metadata_exists = True
            current_remote = max(
                rm.stat_mtime(meta_path), rm.annotation_dir_mtime(prev.uuid)
            )
            remote_changed = current_remote > prev.remote_mtime + 1.0
        else:
            local_changed = True  # device-side deletion — re-upload (never delete)

    if prev and not local_changed and not remote_changed:
        _emit(reporter, "item", "push", "unchanged", key, current, total)
        return

    if prev and local_changed and remote_changed:
        stats.conflicts += 1
        if reporter:
            _emit(
                reporter, "item", "push", "conflict; device wins", key, current, total
            )
        else:
            click.echo(f"  conflict (device wins, will pull): {key}")
        return  # pull phase will refresh local copy

    # If only remote changed, leave local file in place; pull phase handles it.
    if prev and remote_changed and not local_changed:
        _emit(
            reporter,
            "item",
            "push",
            "device newer; queued for pull",
            key,
            current,
            total,
        )
        return

    uid = prev.uuid if prev else _new_uuid()
    _emit(reporter, "item", "tags", "classifying for upload", key, current, total)
    tags = _tag_file(local_path, key, store, stats)
    if tags:
        _emit(
            reporter,
            "item",
            "tags",
            f"tags ready: {', '.join(tags)}",
            key,
            current,
            total,
        )
    else:
        _emit(reporter, "item", "tags", "no tags assigned", key, current, total)
    action = "updating" if prev else "uploading"
    _emit(reporter, "item", "push", action, key, current, total)

    metadata_path = rm.remote_path(f"{uid}.metadata")
    if prev and remote_metadata_exists:
        existing_metadata = rm.get_text(metadata_path)
        existing_tags = xochitl.parse_metadata(existing_metadata)["tags"]
        metadata_text = xochitl.update_document_metadata(
            existing_metadata,
            name=display_name,
            parent=parent_uuid,
            tags=tags or None,
        )
        if not tags and existing_tags:
            tags = existing_tags
            store.set(key, tags, source="device")
    else:
        metadata_text = xochitl.document_metadata(
            name=display_name, parent=parent_uuid, tags=tags
        )

    remote_payload = rm.remote_path(f"{uid}.{file_type}")
    if reporter:
        rm.put_file(
            local_path,
            remote_payload,
            callback=lambda sent, size: _emit(
                reporter, "transfer", "push", action, key, sent, size
            ),
        )
    else:
        rm.put_file(local_path, remote_payload)
    rm.put_text(metadata_path, metadata_text)
    content_path = rm.remote_path(f"{uid}.content")
    if not prev or not rm.exists(content_path):
        rm.put_text(content_path, xochitl.document_content(file_type=file_type))

    remote_mtime = max(
        rm.stat_mtime(rm.remote_path(f"{uid}.metadata")),
        rm.annotation_dir_mtime(uid),
    )
    state.files[key] = FileEntry(
        uuid=uid,
        file_type=file_type,
        size=st.st_size,
        mtime=st.st_mtime,
        remote_mtime=remote_mtime,
        tags=tags,
    )
    _checkpoint(
        state,
        store,
        local_root,
        reporter,
        key,
        current,
        total,
    )
    if prev:
        stats.files_updated += 1
        done_action = "updated"
    else:
        stats.files_uploaded += 1
        done_action = "uploaded"
    detail = f"{key} ({st.st_size / 1024 / 1024:.1f} MB"
    if tags:
        detail += f"; tags: {', '.join(tags)}"
    detail += ")"
    _emit(reporter, "item", "push", done_action, detail, current, total)


def _plan_parallel_upload(
    rm: Remarkable,
    state: State,
    store: TagStore,
    local_root: Path,
    rel: Path,
    parent_uuid: str,
    stats: SyncStats,
    reporter: SyncReporter | None,
    current: int,
    total: int,
) -> _UploadPlan | None:
    """Classify sync state serially before a file is handed to a worker."""
    ext = rel.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        stats.skipped += 1
        _emit(reporter, "item", "push", "unsupported", rel.as_posix(), current, total)
        return None

    local_path = local_root / rel
    st = local_path.stat()
    key = rel.as_posix()
    prev = state.files.get(key)
    local_changed = not prev or prev.size != st.st_size or prev.mtime != st.st_mtime
    remote_changed = False
    if prev:
        meta_path = rm.remote_path(f"{prev.uuid}.metadata")
        if rm.exists(meta_path):
            current_remote = max(
                rm.stat_mtime(meta_path), rm.annotation_dir_mtime(prev.uuid)
            )
            remote_changed = current_remote > prev.remote_mtime + 1.0
        else:
            local_changed = True

    if prev and not local_changed and not remote_changed:
        _emit(reporter, "item", "push", "unchanged", key, current, total)
        return None
    if prev and local_changed and remote_changed:
        stats.conflicts += 1
        _emit(reporter, "item", "push", "conflict; device wins", key, current, total)
        return None
    if prev and remote_changed and not local_changed:
        _emit(
            reporter,
            "item",
            "push",
            "device newer; queued for pull",
            key,
            current,
            total,
        )
        return None

    file_type = ext.lstrip(".")
    return _UploadPlan(
        rel=rel,
        local_path=local_path,
        parent_uuid=parent_uuid,
        uid=prev.uuid if prev else _new_uuid(),
        file_type=file_type,
        display_name=rel.stem,
        size=st.st_size,
        mtime=st.st_mtime,
        cached_tags=tuple(store.get(key)),
        updating=prev is not None,
        current=current,
        total=total,
    )


def _classify_planned_upload(
    plan: _UploadPlan,
    taxonomy: tuple[str, ...],
    reporter: SyncReporter | None,
) -> list[str]:
    key = plan.rel.as_posix()
    _emit(
        reporter,
        "item",
        "tags",
        "classifying for upload",
        key,
        plan.current,
        plan.total,
    )
    if plan.cached_tags:
        tags = list(plan.cached_tags)
    else:
        sample = extract_pdf_text(plan.local_path) if plan.file_type == "pdf" else ""
        tags = categorize(plan.local_path.name, sample, list(taxonomy))
    action = f"tags ready: {', '.join(tags)}" if tags else "no tags assigned"
    _emit(
        reporter,
        "item",
        "tags",
        action,
        key,
        plan.current,
        plan.total,
    )
    return tags


def _transfer_planned_upload(
    rm: Remarkable,
    plan: _UploadPlan,
    tags: list[str],
    reporter: SyncReporter | None,
) -> tuple[FileEntry, bool]:
    key = plan.rel.as_posix()
    action = "updating" if plan.updating else "uploading"
    _emit(
        reporter,
        "item",
        "push",
        action,
        key,
        plan.current,
        plan.total,
    )
    metadata_path = rm.remote_path(f"{plan.uid}.metadata")
    preserve_metadata = plan.updating and rm.exists(metadata_path)
    if preserve_metadata:
        existing_metadata = rm.get_text(metadata_path)
        existing_tags = xochitl.parse_metadata(existing_metadata)["tags"]
        metadata_text = xochitl.update_document_metadata(
            existing_metadata,
            name=plan.display_name,
            parent=plan.parent_uuid,
            tags=tags or None,
        )
        effective_tags = tags or existing_tags
    else:
        metadata_text = xochitl.document_metadata(
            name=plan.display_name, parent=plan.parent_uuid, tags=tags
        )
        effective_tags = tags
    remote_payload = rm.remote_path(f"{plan.uid}.{plan.file_type}")
    if reporter:
        rm.put_file(
            plan.local_path,
            remote_payload,
            callback=lambda sent, size: _emit(
                reporter, "transfer", "push", action, key, sent, size
            ),
        )
    else:
        rm.put_file(plan.local_path, remote_payload)
    rm.put_text(metadata_path, metadata_text)
    content_path = rm.remote_path(f"{plan.uid}.content")
    if not plan.updating or not rm.exists(content_path):
        rm.put_text(content_path, xochitl.document_content(file_type=plan.file_type))
    remote_mtime = max(
        rm.stat_mtime(rm.remote_path(f"{plan.uid}.metadata")),
        rm.annotation_dir_mtime(plan.uid),
    )
    return (
        FileEntry(
            uuid=plan.uid,
            file_type=plan.file_type,
            size=plan.size,
            mtime=plan.mtime,
            remote_mtime=remote_mtime,
            tags=effective_tags,
        ),
        bool(tags),
    )


def _commit_planned_upload(
    plan: _UploadPlan,
    entry: FileEntry,
    state: State,
    store: TagStore,
    local_root: Path,
    stats: SyncStats,
    reporter: SyncReporter | None,
    classified_tags: bool,
) -> None:
    key = plan.rel.as_posix()
    if entry.tags and not plan.cached_tags:
        store.set(key, entry.tags, source="claude" if classified_tags else "device")
        if classified_tags:
            stats.tags_assigned += 1
    state.files[key] = entry
    _checkpoint(
        state,
        store,
        local_root,
        reporter,
        key,
        plan.current,
        plan.total,
    )
    if plan.updating:
        stats.files_updated += 1
        done_action = "updated"
    else:
        stats.files_uploaded += 1
        done_action = "uploaded"
    detail = f"{key} ({plan.size / 1024 / 1024:.1f} MB"
    if entry.tags:
        detail += f"; tags: {', '.join(entry.tags)}"
    detail += ")"
    _emit(
        reporter,
        "item",
        "push",
        done_action,
        detail,
        plan.current,
        plan.total,
    )


def _run_parallel_uploads(
    rm: Remarkable,
    plans: list[_UploadPlan],
    state: State,
    store: TagStore,
    local_root: Path,
    stats: SyncStats,
    reporter: SyncReporter | None,
    workers: int,
) -> None:
    if not plans:
        _emit(reporter, "phase", "push", "Upload queue empty")
        return

    worker_count = min(max(workers, 1), len(plans))
    taxonomy = tuple(store.taxonomy)
    _emit(
        reporter,
        "phase",
        "push",
        f"Uploading {len(plans)} books with {worker_count} parallel SSH workers",
    )

    local_connection = thread_local()
    connections: list[Remarkable] = []
    connections_lock = Lock()
    checkpoint_lock = Lock()

    def connect_worker() -> None:
        worker_rm = Remarkable(rm.cfg)
        worker_rm.connect()
        local_connection.rm = worker_rm
        with connections_lock:
            connections.append(worker_rm)

    def upload_worker(plan: _UploadPlan) -> None:
        worker_rm: Remarkable = local_connection.rm
        tags = _classify_planned_upload(plan, taxonomy, reporter)
        entry, classified_tags = _transfer_planned_upload(
            worker_rm, plan, tags, reporter
        )
        # Checkpoint each completed book before another thread can write state.
        with checkpoint_lock:
            _commit_planned_upload(
                plan,
                entry,
                state,
                store,
                local_root,
                stats,
                reporter,
                classified_tags,
            )

    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="esbern-upload",
            initializer=connect_worker,
        ) as executor:
            futures = [executor.submit(upload_worker, plan) for plan in plans]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    finally:
        for worker_rm in connections:
            worker_rm.close()


def _push(
    rm: Remarkable,
    state: State,
    store: TagStore,
    local_root: Path,
    stats: SyncStats,
    reporter: SyncReporter | None = None,
    workers: int = 1,
) -> None:
    entries = list(_walk_local(local_root))
    folder_count = sum(1 for _, is_dir in entries if is_dir)
    books = [
        rel
        for rel, is_dir in entries
        if not is_dir and rel.suffix.lower() in SUPPORTED_EXTS
    ]
    associated = sum(1 for rel in books if rel.as_posix() in state.files)
    unsupported = len(entries) - folder_count - len(books)
    _emit(
        reporter,
        "phase",
        "push",
        f"Local scan: {len(books)} books, {folder_count} folders; "
        f"{associated} already associated, "
        f"{len(books) - associated} local-only books to upload; "
        f"{unsupported} unsupported files",
    )
    root_uuid = _ensure_root_collection(
        rm, state, name=local_root.name, stats=stats, reporter=reporter
    )
    parents: dict[str, str] = {"": root_uuid}
    if workers > 1 and not hasattr(rm, "cfg"):
        _emit(
            reporter,
            "phase",
            "push",
            "Parallel connections unavailable; using 1 upload worker",
        )
        workers = 1
    plans: list[_UploadPlan] = []

    for index, (rel, is_dir) in enumerate(entries, start=1):
        parent_rel = rel.parent.as_posix() if rel.parent != Path(".") else ""
        parent_uuid = parents.get(parent_rel, root_uuid)
        if is_dir:
            uid = _ensure_subfolder(
                rm, state, rel, parent_uuid, stats, reporter, index, len(entries)
            )
            parents[rel.as_posix()] = uid
            _checkpoint(
                state,
                store,
                local_root,
                reporter,
                rel.as_posix(),
                index,
                len(entries),
            )
        elif workers > 1:
            plan = _plan_parallel_upload(
                rm,
                state,
                store,
                local_root,
                rel,
                parent_uuid,
                stats,
                reporter,
                index,
                len(entries),
            )
            if plan:
                plans.append(plan)
        else:
            _upload_file(
                rm,
                state,
                store,
                local_root,
                rel,
                parent_uuid,
                stats,
                reporter,
                index,
                len(entries),
            )

    if workers > 1:
        _run_parallel_uploads(
            rm,
            plans,
            state,
            store,
            local_root,
            stats,
            reporter,
            workers,
        )


# ---------- tag backfill ----------


def _backfill_tags(
    rm: Remarkable,
    state: State,
    store: TagStore,
    local_root: Path,
    stats: SyncStats,
    reporter: SyncReporter | None = None,
) -> None:
    """Tag files that are already synced but have no tags yet.

    Happens once for any file that landed on the device before tagging
    existed (or while ANTHROPIC_API_KEY was unset). The new tags get
    written to xochitl .metadata so they show up on the reMarkable.
    """
    entries = list(state.files.items())
    _emit(reporter, "phase", "tags", f"Tags: checking {len(entries)} tracked books")
    for index, (relpath, entry) in enumerate(entries, start=1):
        if store.get(relpath):
            _emit(
                reporter, "item", "tags", "tags unchanged", relpath, index, len(entries)
            )
            continue
        local_path = local_root / relpath
        if not local_path.exists():
            _emit(
                reporter,
                "item",
                "tags",
                "missing locally",
                relpath,
                index,
                len(entries),
            )
            continue
        _emit(reporter, "item", "tags", "classifying", relpath, index, len(entries))
        sample = extract_pdf_text(local_path) if entry.file_type == "pdf" else ""
        tags = categorize(local_path.name, sample, store.taxonomy)
        if not tags:
            _emit(
                reporter,
                "item",
                "tags",
                "no tags assigned",
                relpath,
                index,
                len(entries),
            )
            continue
        store.set(relpath, tags)
        entry.tags = tags
        stats.tags_assigned += 1
        meta_path = rm.remote_path(f"{entry.uuid}.metadata")
        try:
            raw = rm.get_text(meta_path)
            rm.put_text(meta_path, xochitl.update_tags(raw, tags))
            entry.remote_mtime = rm.stat_mtime(meta_path)
            _emit(
                reporter,
                "item",
                "tags",
                f"tags updated: {', '.join(tags)}",
                relpath,
                index,
                len(entries),
            )
            _checkpoint(
                state,
                store,
                local_root,
                reporter,
                relpath,
                index,
                len(entries),
            )
        except FileNotFoundError:
            _emit(
                reporter,
                "item",
                "tags",
                "metadata missing on device",
                relpath,
                index,
                len(entries),
            )


# ---------- pull side ----------


@dataclass
class _RemoteNode:
    uid: str
    name: str
    parent: str
    type: str
    deleted: bool
    tags: list[str]
    metadata_mtime: float


def _read_remote_tree(
    rm: Remarkable, reporter: SyncReporter | None = None
) -> dict[str, _RemoteNode]:
    nodes: dict[str, _RemoteNode] = {}
    _emit(reporter, "phase", "scan", "Requesting reMarkable metadata index")
    metadata = _metadata_records(rm)
    _emit(
        reporter,
        "phase",
        "scan",
        f"Read-only scope discovery: checking {len(metadata)} parent links",
    )
    invalid = 0
    for index, (uid, mtime, raw) in enumerate(metadata, start=1):
        try:
            parsed = xochitl.parse_metadata(raw)
        except (ValueError, TypeError, AttributeError):
            invalid += 1
            continue
        nodes[uid] = _RemoteNode(
            uid=uid,
            name=parsed["name"],
            parent=parsed["parent"],
            type=parsed["type"],
            deleted=parsed["deleted"],
            tags=parsed["tags"],
            metadata_mtime=mtime,
        )
        if index == 1 or index % 25 == 0 or index == len(metadata):
            _emit(
                reporter,
                "item",
                "scan",
                "metadata parent links read",
                f"{index} of {len(metadata)}",
                index,
                len(metadata),
            )
    detail = f"Scope discovery complete: {len(nodes)} readable parent links"
    if invalid:
        detail += f", {invalid} unreadable records ignored"
    _emit(reporter, "phase", "scan", detail)
    return nodes


def _descendants(nodes: dict[str, _RemoteNode], root_uid: str) -> dict[str, Path]:
    """Return uid → relpath under the root, for non-deleted nodes."""
    children: dict[str, list[str]] = {}
    for uid, n in nodes.items():
        children.setdefault(n.parent, []).append(uid)

    out: dict[str, Path] = {}
    # BFS from direct children of root_uid.
    stack: list[tuple[str, Path]] = [(c, Path()) for c in children.get(root_uid, [])]
    while stack:
        uid, parent_rel = stack.pop()
        n = nodes.get(uid)
        if n is None or n.deleted or n.parent == "trash":
            continue
        safe = _safe_name(n.name)
        rel = parent_rel / safe
        out[uid] = rel
        if n.type == "CollectionType":
            for c in children.get(uid, []):
                stack.append((c, rel))
    return out


def _document_relpath(rel: Path, suffix: str) -> Path:
    """Add a payload suffix without truncating titles such as ``Version 2.0``."""
    if rel.suffix.lower() in SUPPORTED_EXTS:
        return rel.with_suffix(suffix)
    return rel.parent / f"{rel.name}{suffix}"


def _logical_book_key(rel: Path) -> str:
    """Case-insensitive folder/title identity, independent of PDF vs EPUB."""
    without_book_suffix = (
        rel.with_suffix("") if rel.suffix.lower() in SUPPORTED_EXTS else rel
    )
    return without_book_suffix.as_posix().casefold()


def _unique_local_path(base: Path, rel: Path) -> Path:
    """Resolve name collisions: foo.pdf → foo (2).pdf."""
    candidate = base / rel
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    parent = candidate.parent
    i = 2
    while True:
        c = parent / f"{stem} ({i}){candidate.suffix}"
        if not c.exists():
            return c
        i += 1


def _untracked_local_collisions(
    local_root: Path,
    desired_rel: Path,
    state: State,
    active_remote_uuids: set[str],
) -> list[Path]:
    """Return local books representing a remote title but not a remote UUID."""
    wanted = _logical_book_key(desired_rel)
    tracked = {
        rel.casefold()
        for rel, entry in state.files.items()
        if entry.uuid in active_remote_uuids
    }
    collisions: list[Path] = []
    for path in local_root.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() not in SUPPORTED_EXTS
            or ".esbern" in path.relative_to(local_root).parts
        ):
            continue
        relpath = path.relative_to(local_root)
        if (
            _logical_book_key(relpath) == wanted
            and relpath.as_posix().casefold() not in tracked
        ):
            collisions.append(path)
    return sorted(collisions, key=lambda path: path.as_posix().casefold())


def _checkpoint(
    state: State,
    store: TagStore,
    local_root: Path,
    reporter: SyncReporter | None,
    relpath: str,
    current: int | None,
    total: int | None,
) -> None:
    """Persist completed work so an interrupted sync can resume safely."""
    state.save(local_root)
    store.save()
    _emit(
        reporter,
        "item",
        "finish",
        "state checkpoint saved",
        relpath,
        current,
        total,
    )


def _recover_name_collisions(
    collisions: list[Path],
    *,
    state: State,
    store: TagStore,
    local_root: Path,
    stats: SyncStats,
    active_remote_uuids: set[str],
    reporter: SyncReporter | None,
    current: int,
    total: int,
) -> None:
    if not collisions:
        return
    recovery = move_to_recovery(local_root, collisions, category="sync-collision-trash")
    stats.conflicts += len(collisions)
    for collision in collisions:
        collision_rel = collision.relative_to(local_root).as_posix()
        store.remove(collision_rel)
        for state_rel, entry in list(state.files.items()):
            if (
                state_rel.casefold() == collision_rel.casefold()
                and entry.uuid not in active_remote_uuids
            ):
                state.files.pop(state_rel)
        _emit(
            reporter,
            "item",
            "pull",
            "name collision; device wins",
            f"{collision_rel} → {recovery}",
            current,
            total,
        )


def _pull(
    rm: Remarkable,
    state: State,
    store: TagStore,
    local_root: Path,
    stats: SyncStats,
    *,
    update_remote_tags: bool = True,
    scope_name: str | None = None,
    reporter: SyncReporter | None = None,
) -> set[str]:
    if not state.root_uuid:
        _emit(reporter, "phase", "pull", "reMarkable → local: no linked root")
        return set()
    nodes = _read_remote_tree(rm, reporter)
    if state.root_uuid not in nodes:
        _emit(reporter, "phase", "pull", "reMarkable → local: linked root missing")
        return set()  # root collection missing on device — nothing to pull

    root_node = nodes[state.root_uuid]
    expected_name = scope_name or local_root.name
    valid_root = (
        root_node.type == "CollectionType"
        and root_node.parent == ""
        and not root_node.deleted
        and root_node.name.casefold() == expected_name.casefold()
    )
    if not valid_root:
        _emit(
            reporter,
            "phase",
            "pull",
            f"Scope refused: linked root is not active top-level "
            f"reMarkable/{expected_name}",
        )
        return set()

    remote_rels = _descendants(nodes, state.root_uuid)
    root_name = nodes[state.root_uuid].name or local_root.name
    _emit(
        reporter,
        "phase",
        "pull",
        f"Scoped inventory: reMarkable/{root_name} only "
        f"({len(remote_rels)} descendants)",
    )
    scoped_entries = sorted(
        remote_rels.items(), key=lambda item: item[1].as_posix().casefold()
    )
    for index, (uid, rel) in enumerate(scoped_entries, start=1):
        action = (
            "remote folder"
            if nodes[uid].type == "CollectionType"
            else "remote book"
            if nodes[uid].type == "DocumentType"
            else "remote item"
        )
        _emit(
            reporter,
            "item",
            "scan",
            action,
            rel.as_posix(),
            index,
            len(scoped_entries),
        )
    known_uuids = set(state.folders.values()) | {f.uuid for f in state.files.values()}

    # Materialize new folders so files can land in them. Order by depth.
    folder_uids = [
        u for u, r in remote_rels.items() if nodes[u].type == "CollectionType"
    ]
    folder_uids.sort(key=lambda u: len(remote_rels[u].parts))
    for index, uid in enumerate(folder_uids, start=1):
        rel = remote_rels[uid]
        rel_str = rel.as_posix()
        local_dir = local_root / rel
        local_dir.mkdir(parents=True, exist_ok=True)
        if uid not in known_uuids:
            state.folders[rel_str] = uid
            stats.folders_pulled += 1
            action = "folder created locally"
        else:
            action = "folder unchanged"
        state.folder_remote_mtime[rel_str] = nodes[uid].metadata_mtime
        _emit(reporter, "item", "pull", action, rel_str, index, len(folder_uids))

    # Build reverse map for files we've already seen (uid → relpath).
    file_uid_to_rel = {f.uuid: rel for rel, f in state.files.items()}

    documents = [
        (uid, rel)
        for uid, rel in remote_rels.items()
        if nodes[uid].type == "DocumentType"
    ]
    active_remote_uuids = set(remote_rels)
    _emit(
        reporter, "phase", "pull", f"Remote books: checking {len(documents)} documents"
    )
    for index, (uid, rel) in enumerate(documents, start=1):
        node = nodes[uid]

        ext = ".pdf"  # we set this from .content below if needed
        content_path = rm.remote_path(f"{uid}.content")
        try:
            content_raw = rm.get_text(content_path)
            import json as _json

            ft = (_json.loads(content_raw).get("fileType") or "pdf").lower()
            if ft in {"pdf", "epub"}:
                ext = f".{ft}"
        except (OSError, ValueError, TypeError, AttributeError):
            ft = "pdf"

        remote_payload = rm.remote_path(f"{uid}.{ft}")
        if not rm.exists(remote_payload):
            _emit(
                reporter,
                "item",
                "pull",
                "no PDF/EPUB payload",
                rel.as_posix(),
                index,
                len(documents),
            )
            continue  # nothing to download (e.g. a notebook with no PDF payload)

        annot_mtime = rm.annotation_dir_mtime(uid)
        meta_mtime = node.metadata_mtime
        latest_remote = max(meta_mtime, annot_mtime)

        if uid in file_uid_to_rel:
            # Known doc — refresh only if the device has newer state.
            relpath = file_uid_to_rel[uid]
            entry = state.files[relpath]
            local_path = local_root / relpath
            local_missing = not local_path.exists()
            remote_changed = latest_remote > entry.remote_mtime + 1.0
            if not remote_changed and not local_missing:
                _emit(
                    reporter,
                    "item",
                    "pull",
                    "unchanged",
                    relpath,
                    index,
                    len(documents),
                )
                continue
            if remote_changed and not local_missing:
                local_stat = local_path.stat()
                local_changed = (
                    local_stat.st_size != entry.size
                    or local_stat.st_mtime != entry.mtime
                )
                if local_changed:
                    stats.conflicts += 1
                    _emit(
                        reporter,
                        "item",
                        "pull",
                        "conflict; device wins",
                        relpath,
                        index,
                        len(documents),
                    )
            refresh_action = (
                "restoring missing local file"
                if local_missing
                else "refreshing from device"
            )
            _emit(
                reporter,
                "item",
                "pull",
                refresh_action,
                relpath,
                index,
                len(documents),
            )
            if reporter:
                rm.get_file(
                    remote_payload,
                    local_path,
                    callback=lambda received, size, name=relpath: _emit(
                        reporter,
                        "transfer",
                        "pull",
                        "downloading",
                        name,
                        received,
                        size,
                    ),
                )
            else:
                rm.get_file(remote_payload, local_path)
            st = local_path.stat()
            entry.size = st.st_size
            entry.mtime = st.st_mtime
            entry.remote_mtime = latest_remote
            entry.tags = node.tags or entry.tags
            stats.files_repulled += 1
            _checkpoint(
                state,
                store,
                local_root,
                reporter,
                relpath,
                index,
                len(documents),
            )
            _emit(
                reporter,
                "item",
                "pull",
                "refreshed",
                f"{relpath} ({st.st_size / 1024 / 1024:.1f} MB)",
                index,
                len(documents),
            )
        else:
            # New to us — materialize on disk. A local file with the same
            # folder/title is not uploaded as a second document: the device
            # copy wins and the displaced local copy remains recoverable.
            desired_rel = _document_relpath(rel, ext)
            desired_key = _logical_book_key(desired_rel)
            tracked_keys = {
                _logical_book_key(Path(existing_rel))
                for existing_rel, entry in state.files.items()
                if entry.uuid in active_remote_uuids
            }
            if desired_key in tracked_keys:
                # Two distinct remote UUIDs have the same visible path. Keep
                # both local representations; never merge remote documents.
                local_path = _unique_local_path(local_root, desired_rel)
                collisions: list[Path] = []
            else:
                local_path = local_root / desired_rel
                collisions = _untracked_local_collisions(
                    local_root, desired_rel, state, active_remote_uuids
                )
            local_path.parent.mkdir(parents=True, exist_ok=True)
            relpath = local_path.relative_to(local_root).as_posix()

            matching_local = next(
                (
                    collision
                    for collision in collisions
                    if collision.suffix.lower() == ext
                    and collision.relative_to(local_root).as_posix().casefold()
                    == desired_rel.as_posix().casefold()
                ),
                None,
            )
            if matching_local is not None:
                local_size = matching_local.stat().st_size
                remote_size = rm.stat_size(remote_payload)
                _emit(
                    reporter,
                    "item",
                    "pull",
                    "checking existing local copy",
                    f"{relpath} ({local_size} local bytes; "
                    f"{remote_size if remote_size is not None else 'unknown'} "
                    f"device bytes)",
                    index,
                    len(documents),
                )
                payload_matches = False
                if remote_size is not None and local_size == remote_size:
                    _emit(
                        reporter,
                        "item",
                        "pull",
                        "verifying existing checksum",
                        relpath,
                        index,
                        len(documents),
                    )
                    remote_digest = rm.file_sha256(remote_payload)
                    if remote_digest is None:
                        payload_matches = True
                        _emit(
                            reporter,
                            "item",
                            "pull",
                            "device checksum unavailable; exact path and size match",
                            relpath,
                            index,
                            len(documents),
                        )
                    else:
                        payload_matches = file_sha256(matching_local) == remote_digest
                        if not payload_matches:
                            _emit(
                                reporter,
                                "item",
                                "pull",
                                "checksum differs; device copy will replace local",
                                relpath,
                                index,
                                len(documents),
                            )
                if payload_matches:
                    other_collisions = [
                        collision
                        for collision in collisions
                        if collision != matching_local
                    ]
                    _recover_name_collisions(
                        other_collisions,
                        state=state,
                        store=store,
                        local_root=local_root,
                        stats=stats,
                        active_remote_uuids=active_remote_uuids,
                        reporter=reporter,
                        current=index,
                        total=len(documents),
                    )
                    local_path = matching_local
                    relpath = local_path.relative_to(local_root).as_posix()
                    if node.tags:
                        tags = node.tags
                        store.set(relpath, tags, source="device")
                        _emit(
                            reporter,
                            "item",
                            "tags",
                            f"using device tags: {', '.join(tags)}",
                            relpath,
                            index,
                            len(documents),
                        )
                    else:
                        tags = store.get(relpath)
                    st = local_path.stat()
                    state.files[relpath] = FileEntry(
                        uuid=uid,
                        file_type=ft,
                        size=st.st_size,
                        mtime=st.st_mtime,
                        remote_mtime=latest_remote,
                        tags=tags,
                    )
                    stats.files_linked += 1
                    _emit(
                        reporter,
                        "item",
                        "pull",
                        "already present; linked without download",
                        f"{relpath} ({st.st_size / 1024 / 1024:.1f} MB)",
                        index,
                        len(documents),
                    )
                    _checkpoint(
                        state,
                        store,
                        local_root,
                        reporter,
                        relpath,
                        index,
                        len(documents),
                    )
                    continue

            _emit(
                reporter,
                "item",
                "pull",
                "downloading new",
                relpath,
                index,
                len(documents),
            )
            incoming_path = local_root / ".esbern" / "incoming" / f"{uid}{ext}"
            if reporter:
                rm.get_file(
                    remote_payload,
                    incoming_path,
                    callback=lambda received, size, name=relpath: _emit(
                        reporter,
                        "transfer",
                        "pull",
                        "downloading",
                        name,
                        received,
                        size,
                    ),
                )
            else:
                rm.get_file(remote_payload, incoming_path)
            _recover_name_collisions(
                collisions,
                state=state,
                store=store,
                local_root=local_root,
                stats=stats,
                active_remote_uuids=active_remote_uuids,
                reporter=reporter,
                current=index,
                total=len(documents),
            )
            incoming_path.replace(local_path)
            st = local_path.stat()

            # Tag: prefer device tags if present; otherwise call the LLM.
            # A two-way sync reflects new tags back to the device; a pull
            # keeps the remote side strictly read-only.
            if node.tags:
                tags = node.tags
                store.set(relpath, tags, source="device")
                _emit(
                    reporter,
                    "item",
                    "tags",
                    f"using device tags: {', '.join(tags)}",
                    relpath,
                    index,
                    len(documents),
                )
            else:
                _emit(
                    reporter,
                    "item",
                    "tags",
                    "classifying downloaded book",
                    relpath,
                    index,
                    len(documents),
                )
                sample = extract_pdf_text(local_path) if ext == ".pdf" else ""
                tags = categorize(local_path.name, sample, store.taxonomy)
                if tags:
                    store.set(relpath, tags)
                    stats.tags_assigned += 1
                    _emit(
                        reporter,
                        "item",
                        "tags",
                        f"tags assigned: {', '.join(tags)}",
                        relpath,
                        index,
                        len(documents),
                    )
                    if update_remote_tags:
                        _emit(
                            reporter,
                            "item",
                            "tags",
                            "writing tags to device",
                            relpath,
                            index,
                            len(documents),
                        )
                        meta_raw = rm.get_text(rm.remote_path(f"{uid}.metadata"))
                        rm.put_text(
                            rm.remote_path(f"{uid}.metadata"),
                            xochitl.update_tags(meta_raw, tags),
                        )
                        latest_remote = rm.stat_mtime(rm.remote_path(f"{uid}.metadata"))
                else:
                    _emit(
                        reporter,
                        "item",
                        "tags",
                        "no tags assigned",
                        relpath,
                        index,
                        len(documents),
                    )

            state.files[relpath] = FileEntry(
                uuid=uid,
                file_type=ft,
                size=st.st_size,
                mtime=st.st_mtime,
                remote_mtime=latest_remote,
                tags=tags,
            )
            stats.files_pulled += 1
            detail = f"{relpath} ({st.st_size / 1024 / 1024:.1f} MB"
            if tags:
                detail += f"; tags: {', '.join(tags)}"
            detail += ")"
            _emit(reporter, "item", "pull", "downloaded", detail, index, len(documents))
            _checkpoint(
                state,
                store,
                local_root,
                reporter,
                relpath,
                index,
                len(documents),
            )
    return active_remote_uuids


def _prune_inactive_file_mappings(
    state: State,
    store: TagStore,
    local_root: Path,
    active_remote_uuids: set[str],
    reporter: SyncReporter | None = None,
) -> None:
    """Forget file UUIDs that no longer belong to the active remote root."""
    pruned = False
    removed_tags = False
    for relpath, entry in list(state.files.items()):
        if entry.uuid in active_remote_uuids:
            continue
        local_path = local_root / relpath
        local_exists = local_path.is_file() and not local_path.is_symlink()
        state.files.pop(relpath)
        pruned = True
        if not local_exists:
            store.remove(relpath)
            removed_tags = True
        action = (
            "stale file mapping pruned; local copy will upload"
            if local_exists
            else "stale file mapping pruned; missing on both sides"
        )
        _emit(
            reporter,
            "item",
            "pull",
            action,
            f"{relpath} ({entry.uuid[:8]})",
        )
    if pruned:
        state.save(local_root)
    if removed_tags:
        store.save()


# ---------- pre-push dedup ----------


def _deduplicate_local(
    state: State,
    store: TagStore,
    local_root: Path,
    stats: SyncStats,
    active_remote_uuids: set[str],
    reporter: SyncReporter | None = None,
) -> None:
    """Remove upload candidates duplicated by local or remote-backed files."""
    _emit(
        reporter,
        "phase",
        "dedup",
        "Deduplication: comparing byte-identical PDF/EPUB candidates",
    )

    def scanned(path: Path, index: int, total: int) -> None:
        _emit(
            reporter,
            "item",
            "dedup",
            "hashing",
            path.relative_to(local_root).as_posix(),
            index,
            total,
        )

    tracked = {
        relpath
        for relpath, entry in state.files.items()
        if entry.uuid in active_remote_uuids
    }
    groups = find_duplicate_groups(local_root, scanned, tracked_relpaths=tracked)
    safe_groups: list[DuplicateGroup] = []
    for group in groups:
        members = (group.keeper, *group.duplicates)
        tracked_members = [
            path
            for path in members
            if path.relative_to(local_root).as_posix() in tracked
        ]
        if len(tracked_members) > 1:
            # Each tracked path represents a separate remote UUID. Preserve
            # every remote document, but still remove extra local-only copies.
            untracked_members = tuple(
                path for path in members if path not in tracked_members
            )
            for path in tracked_members:
                _emit(
                    reporter,
                    "item",
                    "dedup",
                    "duplicate remote entry preserved",
                    path.relative_to(local_root).as_posix(),
                )
            if untracked_members:
                safe_groups.append(
                    DuplicateGroup(
                        keeper=tracked_members[0],
                        duplicates=untracked_members,
                        size=group.size,
                    )
                )
            continue
        safe_groups.append(group)

    if not safe_groups:
        _emit(reporter, "phase", "dedup", "Deduplication: no removable duplicates")
        return

    for group in safe_groups:
        keeper_rel = group.keeper.relative_to(local_root).as_posix()
        keeper_action = (
            "keeping remote-backed copy" if keeper_rel in tracked else "keeping copy"
        )
        _emit(reporter, "item", "dedup", keeper_action, keeper_rel)
        for duplicate in group.duplicates:
            _emit(
                reporter,
                "item",
                "dedup",
                "moving duplicate to recovery",
                duplicate.relative_to(local_root).as_posix(),
            )

    result = deduplicate(local_root, tuple(safe_groups))
    for group in safe_groups:
        for duplicate in group.duplicates:
            duplicate_rel = duplicate.relative_to(local_root).as_posix()
            store.remove(duplicate_rel)
            state.files.pop(duplicate_rel, None)
    stats.duplicates_removed += result.files_removed
    stats.duplicate_bytes_removed += result.bytes_removed
    state.save(local_root)
    store.save()
    _emit(
        reporter,
        "phase",
        "dedup",
        f"Deduplication complete: moved {result.files_removed} files "
        f"({result.bytes_removed / 1024 / 1024:.1f} MB) to "
        f"{result.trash_directory}",
    )


# ---------- entry point ----------


def _resolve_pull_root(
    rm: Remarkable,
    state: State,
    name: str,
    *,
    require_name_match: bool = False,
    reporter: SyncReporter | None = None,
) -> str:
    """Return an active Collection to pull, linking by name when needed."""
    _emit(reporter, "phase", "setup", f"Resolving remote root '{name}'")
    previous_root = state.root_uuid
    if state.root_uuid:
        meta_path = rm.remote_path(f"{state.root_uuid}.metadata")
        if rm.exists(meta_path):
            try:
                parsed = xochitl.parse_metadata(rm.get_text(meta_path))
            except (OSError, ValueError, TypeError, AttributeError):
                parsed = None
            if (
                parsed
                and parsed["type"] == "CollectionType"
                and not parsed["deleted"]
                and parsed["parent"] != "trash"
                and (
                    not require_name_match
                    or parsed["name"].casefold() == name.casefold()
                )
            ):
                _emit(
                    reporter,
                    "item",
                    "setup",
                    "root linked",
                    f"{parsed['name']} ({state.root_uuid[:8]})",
                )
                return state.root_uuid

    existing = _find_existing_root_collection(rm, name)
    if not existing:
        raise click.ClickException(
            f"No active top-level reMarkable folder matches '{name}'."
        )
    state.root_uuid = existing
    if previous_root and previous_root != existing:
        # UUID mappings are scoped to their old root. Keeping them would make
        # a later two-way sync believe those documents already live under the
        # newly linked Collection and skip their upload.
        state.folders.clear()
        state.folder_remote_mtime.clear()
        state.files.clear()
        message = f"{name} ({existing[:8]}); reset stale item mappings"
        if reporter:
            _emit(reporter, "item", "setup", "root relinked", message)
        else:
            click.echo(f"  relinked active reMarkable folder: {message}")
    else:
        message = f"{name} ({existing[:8]})"
        if reporter:
            _emit(reporter, "item", "setup", "root linked", message)
        else:
            click.echo(f"  linked existing reMarkable folder: {message}")
    return existing


def pull(
    rm: Remarkable,
    local_root: Path,
    remote_name: str | None = None,
    reporter: SyncReporter | None = None,
) -> SyncStats:
    """One-way sync from an existing reMarkable Collection to local disk."""
    _emit(reporter, "phase", "setup", f"Loading state from {local_root}")
    state = State.load(local_root)
    store = TagStore.load()
    store.scope_to(local_root)
    stats = SyncStats()

    name = remote_name or local_root.name
    _resolve_pull_root(
        rm,
        state,
        name,
        require_name_match=remote_name is not None,
        reporter=reporter,
    )
    local_root.mkdir(parents=True, exist_ok=True)
    state.save(local_root)
    _pull(
        rm,
        state,
        store,
        local_root,
        stats,
        update_remote_tags=False,
        scope_name=name,
        reporter=reporter,
    )

    _emit(reporter, "phase", "finish", "Saving local pull state")
    state.save(local_root)
    store.save()
    _emit(reporter, "phase", "finish", "Pull complete")
    return stats


def sync(
    rm: Remarkable,
    local_root: Path,
    restart: bool = True,
    reporter: SyncReporter | None = None,
    workers: int = 1,
) -> SyncStats:
    _emit(reporter, "phase", "setup", f"Loading state from {local_root}")
    state = State.load(local_root)
    store = TagStore.load()
    store.scope_to(local_root)
    stats = SyncStats()
    if state.files or state.folders:
        _emit(
            reporter,
            "item",
            "setup",
            "resume checkpoint loaded",
            f"{len(state.files)} books, {len(state.folders)} folders",
        )

    try:
        _emit(reporter, "phase", "setup", "Linking the reMarkable root before pull")
        _ensure_root_collection(
            rm, state, name=local_root.name, stats=stats, reporter=reporter
        )
        state.save(local_root)
        active_remote_uuids = _pull(
            rm, state, store, local_root, stats, reporter=reporter
        )
        _prune_inactive_file_mappings(
            state,
            store,
            local_root,
            active_remote_uuids,
            reporter,
        )
        _deduplicate_local(
            state, store, local_root, stats, active_remote_uuids, reporter
        )
        _push(rm, state, store, local_root, stats, reporter, workers=workers)
        _backfill_tags(rm, state, store, local_root, stats, reporter)

        _emit(reporter, "phase", "finish", "Saving sync state and tags")
        state.save(local_root)
        store.save()
    except BaseException:
        changed = (
            stats.files_uploaded
            or stats.files_updated
            or stats.folders_created
            or stats.tags_assigned
        )
        if restart and changed:
            _emit(
                reporter,
                "phase",
                "finish",
                "Sync interrupted; refreshing reMarkable document service",
            )
            try:
                rm.restart_xochitl()
            except Exception as exc:  # noqa: BLE001 - preserve the original failure
                _emit(
                    reporter,
                    "item",
                    "finish",
                    "document service refresh failed",
                    str(exc),
                )
        raise

    if restart and (
        stats.files_uploaded
        or stats.files_updated
        or stats.folders_created
        or stats.tags_assigned
    ):
        _emit(reporter, "phase", "finish", "Restarting reMarkable document service")
        rm.restart_xochitl()
    else:
        _emit(reporter, "phase", "finish", "No device restart required")
    _emit(reporter, "phase", "finish", "Sync complete")
    return stats
