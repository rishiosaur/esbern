from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import RLock

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from esbern import __version__, config
from esbern.book_metadata import BookMetadataError, google_books_api_key
from esbern.dedup import (
    DeduplicationChangedError,
    deduplicate,
    find_duplicate_groups,
)
from esbern.downloader import (
    SUPPORTED_FORMATS,
    BookDownloadError,
    download_book,
    find_existing_book,
    read_bulk_queries,
)
from esbern.library_metadata import (
    apply_library_metadata,
    plan_library_metadata,
)
from esbern.remarkable import connected
from esbern.state import State
from esbern.sync import SyncEvent, _safe_name
from esbern.sync import pull as run_pull
from esbern.sync import sync as run_sync
from esbern.tags import TAGS_PATH, TagStore


@click.group(help="Two-way sync between a local folder and a reMarkable Paper Pro.")
@click.version_option(__version__)
def main() -> None:
    pass


class _VerboseSyncDisplay:
    def __init__(self, console: Console):
        self.console = console
        self.transfer = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            DownloadColumn(binary_units=True),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
            refresh_per_second=10,
        )
        self._transfer_tasks: dict[str, int] = {}
        self._phase_task: int | None = None
        self._lock = RLock()

    def _finish_phase(self) -> None:
        if self._phase_task is not None:
            self.transfer.remove_task(self._phase_task)
            self._phase_task = None

    def _finish_transfers(self) -> None:
        for task_id in self._transfer_tasks.values():
            self.transfer.remove_task(task_id)
        self._transfer_tasks.clear()

    def _finish_item_transfer(self, item: str) -> None:
        for transfer_item, task_id in list(self._transfer_tasks.items()):
            if item == transfer_item or item.startswith(f"{transfer_item} ("):
                self.transfer.remove_task(task_id)
                self._transfer_tasks.pop(transfer_item, None)

    def __call__(self, event: SyncEvent) -> None:
        with self._lock:
            if event.kind == "transfer":
                self._finish_phase()
                task_id = self._transfer_tasks.get(event.item)
                if task_id is None:
                    task_id = self.transfer.add_task(
                        f"{event.action}: {event.item}",
                        total=event.total or 0,
                    )
                    self._transfer_tasks[event.item] = task_id
                self.transfer.update(
                    task_id,
                    completed=event.current or 0,
                    total=event.total or 0,
                )
                return

            self._finish_phase()
            if event.kind == "phase":
                self._finish_transfers()
                self.console.print(f"\n{event.action}", style="bold cyan", markup=False)
                self._phase_task = self.transfer.add_task(event.action, total=None)
                return

            if event.action in {"uploaded", "updated", "downloaded", "refreshed"}:
                self._finish_item_transfer(event.item)
            position = ""
            if event.current is not None and event.total is not None:
                position = f"[{event.current}/{event.total}] "
            line = f"  {position}{event.action}: {event.item}"
            action = event.action.casefold()
            if "conflict" in action or "invalid" in action:
                style = "red"
            elif any(
                word in action
                for word in (
                    "upload",
                    "download",
                    "created",
                    "updated",
                    "refreshed",
                    "restored",
                    "adopted",
                )
            ):
                style = "green"
            elif any(
                word in action
                for word in (
                    "unsupported",
                    "missing",
                    "no tags",
                    "device newer",
                    "duplicate",
                    "collision",
                )
            ):
                style = "yellow"
            else:
                style = "dim"
            self.console.print(line, style=style, markup=False)

    def close(self) -> None:
        with self._lock:
            self._finish_phase()
            self._finish_transfers()


@main.command(help="Configure the connection to your reMarkable.")
@click.option("--host", default="10.11.99.1", show_default=True)
@click.option("--port", default=22, show_default=True, type=int)
@click.option("--user", default="root", show_default=True)
@click.option(
    "--password",
    default=None,
    help="SSH password from the device (Settings → Help → Copyrights and licenses).",
)
@click.option("--key-path", default=None, type=click.Path())
@click.option("--no-restart", is_flag=True, help="Don't restart xochitl after syncing.")
def init(
    host: str,
    port: int,
    user: str,
    password: str | None,
    key_path: str | None,
    no_restart: bool,
) -> None:
    if not password and not key_path:
        password = (
            click.prompt(
                "SSH password", hide_input=True, default="", show_default=False
            )
            or None
        )
    cfg = config.Config(
        host=host,
        port=port,
        user=user,
        password=password,
        key_path=key_path,
        restart_xochitl=not no_restart,
    )
    config.save(cfg)
    click.echo(f"Wrote {config.CONFIG_PATH}")


@main.command(help="Test the SSH connection to the reMarkable.")
def ping() -> None:
    cfg = config.load()
    with connected(cfg) as rm:
        rc, out, _ = rm.exec("cat /etc/version 2>/dev/null; uname -a")
        click.echo(out.strip() if rc == 0 else "connected")


@main.command(help="Two-way sync between the current directory and the reMarkable.")
@click.option("--path", default=".", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--workers",
    default=4,
    show_default=True,
    type=click.IntRange(1, 8),
    envvar="ESBERN_SYNC_WORKERS",
    help="Parallel SSH workers for classification and uploads.",
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would change locally; don't connect."
)
def sync(path: str, workers: int, dry_run: bool) -> None:
    root = Path(path).resolve()
    if dry_run:
        _print_dry_run(root)
        return
    cfg = config.load()
    console = Console()
    display = _VerboseSyncDisplay(console)
    operation_started = connection_started = time.monotonic()
    console.print(
        f"Connecting to {cfg.user}@{cfg.host} for {root} ↔ reMarkable/{root.name}…",
        style="bold cyan",
        markup=False,
    )
    with (
        console.status("Opening SSH and SFTP sessions…", spinner="dots") as status,
        connected(cfg) as rm,
    ):
        status.stop()
        console.print(
            f"Connected to {cfg.user}@{cfg.host} "
            f"in {time.monotonic() - connection_started:.1f}s.",
            style="green",
            markup=False,
        )
        with display.transfer:
            stats = run_sync(
                rm,
                root,
                restart=cfg.restart_xochitl,
                reporter=display,
                workers=workers,
            )
            display.close()
    console.print(
        f"Disconnected from {cfg.host}. Total time: "
        f"{time.monotonic() - operation_started:.1f}s.",
        style="dim",
        markup=False,
    )
    console.print("\nSync summary", style="bold cyan", markup=False)
    console.print(
        f"Push: {stats.files_uploaded} new, {stats.files_updated} updated, "
        f"{stats.folders_created} folders.\n"
        f"Pull: {stats.files_pulled} new, {stats.files_repulled} updated, "
        f"{stats.files_linked} already local, {stats.folders_pulled} folders.\n"
        f"Tags assigned: {stats.tags_assigned}. "
        f"Conflicts (device won): {stats.conflicts}. "
        f"Skipped: {stats.skipped}.\n"
        f"Dedup: {stats.duplicates_removed} local duplicates moved "
        f"({stats.duplicate_bytes_removed / 1024 / 1024:.1f} MB)."
    )


@main.command(help="Pull new and changed files from the reMarkable into this folder.")
@click.argument("remote_folder", required=False)
@click.option("--path", default=".", type=click.Path(exists=True, file_okay=False))
def pull(remote_folder: str | None, path: str) -> None:
    destination = Path(path).resolve()
    root = destination / _safe_name(remote_folder) if remote_folder else destination
    remote_name = remote_folder or destination.name
    cfg = config.load()
    console = Console()
    display = _VerboseSyncDisplay(console)
    operation_started = connection_started = time.monotonic()
    console.print(
        f"Connecting to {cfg.user}@{cfg.host} to pull "
        f"reMarkable/{remote_name} → {root}…",
        style="bold cyan",
        markup=False,
    )
    with (
        console.status("Opening SSH and SFTP sessions…", spinner="dots") as status,
        connected(cfg) as rm,
    ):
        status.stop()
        console.print(
            f"Connected to {cfg.user}@{cfg.host} "
            f"in {time.monotonic() - connection_started:.1f}s.",
            style="green",
            markup=False,
        )
        with display.transfer:
            stats = run_pull(rm, root, remote_name=remote_folder, reporter=display)
            display.close()
    console.print(
        f"Disconnected from {cfg.host}. Total time: "
        f"{time.monotonic() - operation_started:.1f}s.",
        style="dim",
        markup=False,
    )
    console.print("\nPull summary", style="bold cyan", markup=False)
    console.print(
        f"Pull: {stats.files_pulled} new, {stats.files_repulled} updated, "
        f"{stats.files_linked} already local, {stats.folders_pulled} folders. "
        f"Tags assigned locally: {stats.tags_assigned}."
    )


@main.command("get", help="Find and download a book into the local sync folder.")
@click.argument("terms", nargs=-1)
@click.option(
    "-b",
    "--bulk",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read one book/search query per line from this file.",
)
@click.option(
    "--path",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Destination sync folder.",
)
@click.option(
    "--format",
    "format_",
    type=click.Choice(["auto", "epub", "pdf"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Preferred download format; auto tries EPUB, then PDF.",
)
@click.option(
    "-j",
    "--jobs",
    type=click.IntRange(1, 32),
    default=4,
    show_default=True,
    help="Parallel workers for bulk downloads.",
)
@click.option(
    "--source",
    type=click.Choice(["auto", "libgen", "arxiv"], case_sensitive=False),
    default="libgen",
    show_default=True,
    help="Download source; auto enables LibGen/arXiv fallback.",
)
@click.option(
    "--google-books-key",
    envvar="GOOGLE_BOOKS_API_KEY",
    help="Google Books API key (prefer the GOOGLE_BOOKS_API_KEY environment variable).",
)
@click.option(
    "--metadata/--no-metadata",
    default=True,
    show_default=True,
    help="Enrich LibGen downloads and apply Author(s) - Title (year) filenames.",
)
def get_book(
    terms: tuple[str, ...],
    bulk: Path | None,
    path: Path,
    format_: str,
    jobs: int,
    source: str,
    google_books_key: str | None,
    metadata: bool,
) -> None:
    query = " ".join(terms).strip()
    if bulk and query:
        raise click.UsageError("Pass either search terms or --bulk, not both.")
    if not bulk and not query:
        raise click.UsageError("Pass book/search terms or use --bulk INPUT.txt.")
    google_books_key = google_books_api_key(google_books_key)
    if metadata and source.lower() != "arxiv" and not google_books_key:
        raise click.UsageError(
            "Google Books metadata is enabled but no API key is configured. "
            "Set GOOGLE_BOOKS_API_KEY, pass --google-books-key, or explicitly "
            "use --no-metadata."
        )

    formats = SUPPORTED_FORMATS if format_.lower() == "auto" else (format_.lower(),)
    destination = path.resolve()
    console = Console()

    if not bulk:
        try:
            result = download_book(
                query,
                destination,
                formats=formats,
                console=console,
                source=source.lower(),
                google_books_api_key=google_books_key,
                enrich_metadata=metadata,
            )
        except BookDownloadError as error:
            raise click.ClickException(str(error)) from error
        console.print(
            f"Downloaded {result.path.name} ({result.format.upper()}, "
            f"{result.source}) to {result.path.parent}",
            style="green",
            markup=False,
        )
        return

    try:
        queries = read_bulk_queries(bulk)
    except (OSError, UnicodeError) as error:
        raise click.ClickException(f"Could not read {bulk}: {error}") from error
    if not queries:
        raise click.ClickException(f"No book queries found in {bulk}.")

    pending: list[tuple[int, str]] = []
    skipped: list[tuple[str, Path | None]] = []
    seen_queries: set[str] = set()
    for index, item in enumerate(queries, start=1):
        query_key = " ".join(item.casefold().split())
        if query_key in seen_queries:
            skipped.append((item, None))
            console.print(
                f"Skip [{index}/{len(queries)}] duplicate query: {item}",
                style="yellow",
                markup=False,
            )
            continue
        seen_queries.add(query_key)
        existing = find_existing_book(item, destination)
        if existing:
            skipped.append((item, existing))
            console.print(
                f"Skip [{index}/{len(queries)}] already exists: {existing.name}",
                style="yellow",
                markup=False,
            )
            continue
        pending.append((index, item))

    worker_count = min(jobs, len(pending))
    console.print(
        f"Downloading {len(pending)} books to {destination} "
        f"with {worker_count} worker{'s' if worker_count != 1 else ''}",
        markup=False,
    )
    succeeded: list[Path] = []
    failed: list[tuple[str, str]] = []

    if pending:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", markup=False),
            TextColumn("{task.fields[status]}", markup=False),
            TextColumn("{task.fields[detail]}", markup=False),
            TimeElapsedColumn(),
            console=console,
            refresh_per_second=8,
        )
        task_ids = {
            index: progress.add_task(
                f"[{index}/{len(queries)}] {item}",
                total=None,
                status="Queued",
                detail="",
                visible=False,
            )
            for index, item in pending
        }

        def download_one(index: int, item: str):
            task_id = task_ids[index]
            progress.update(task_id, visible=True, status="Starting", detail="")

            def report(status: str | None, detail: str | None) -> None:
                changes = {}
                if status is not None:
                    changes["status"] = status
                if detail is not None:
                    changes["detail"] = detail
                progress.update(task_id, **changes)

            # The source-specific Rich display is rendered into memory while
            # its structured updates drive the shared terminal display.
            worker_console = Console(
                file=io.StringIO(), force_terminal=False, color_system=None
            )
            return download_book(
                item,
                destination,
                formats=formats,
                console=worker_console,
                source=source.lower(),
                progress_callback=report,
                google_books_api_key=google_books_key,
                enrich_metadata=metadata,
            )

        with progress, ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(download_one, index, item): (index, item)
                for index, item in pending
            }
            for future in as_completed(futures):
                index, item = futures[future]
                progress.remove_task(task_ids[index])
                try:
                    result = future.result()
                    succeeded.append(result.path)
                    console.print(
                        f"Done [{index}/{len(queries)}] {result.path.name} "
                        f"({result.format.upper()}, {result.source})",
                        style="green",
                        markup=False,
                    )
                except BookDownloadError as error:
                    failed.append((item, str(error)))
                    console.print(
                        f"Failed [{index}/{len(queries)}] {item}: {error}",
                        style="red",
                        markup=False,
                    )

    console.print(
        f"\nBulk complete: {len(succeeded)} downloaded, {len(failed)} failed, "
        f"{len(skipped)} skipped."
    )
    if failed:
        raise click.exceptions.Exit(1)


@main.command(
    "normalize",
    help="Enrich tracked books and rename them without changing reMarkable UUIDs.",
)
@click.option(
    "--path",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Synced book folder to normalize.",
)
@click.option(
    "--google-books-key",
    envvar="GOOGLE_BOOKS_API_KEY",
    help="Google Books API key (prefer the GOOGLE_BOOKS_API_KEY environment variable).",
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Apply the plan locally and on the reMarkable; otherwise preview only.",
)
@click.option(
    "--allow-unmatched",
    is_flag=True,
    help="Apply resolved books even when other files need manual metadata.",
)
@click.option(
    "--backup-files/--no-backup-files",
    default=True,
    show_default=True,
    help="Keep recoverable copies of original local payloads.",
)
def normalize_library(
    path: Path,
    google_books_key: str | None,
    apply_changes: bool,
    allow_unmatched: bool,
    backup_files: bool,
) -> None:
    google_books_key = google_books_api_key(google_books_key)
    if not google_books_key:
        raise click.UsageError("Set GOOGLE_BOOKS_API_KEY or pass --google-books-key.")
    # Preserve the final path component so the normalization preflight can
    # reject a library root that is itself a symlink.
    root = path.expanduser().absolute()
    state = State.load(root)
    if not state.root_uuid or not state.files:
        raise click.ClickException(
            f"{root} has no tracked Esbern library state to normalize."
        )
    console = Console()

    def report(action: str, detail: str) -> None:
        style = "green" if action in {"planned", "updated"} else "cyan"
        console.print(f"{action.capitalize()}: {detail}", style=style, markup=False)

    console.print(
        f"Resolving Google Books metadata for {len(state.files)} tracked books…",
        style="bold cyan",
        markup=False,
    )
    plans, failures = plan_library_metadata(
        root,
        api_key=google_books_key,
        reporter=report,
    )
    if failures:
        console.print(
            f"\n{len(failures)} file(s) need manual review:",
            style="yellow",
            markup=False,
        )
        for failure in failures:
            console.print(
                f"  {failure.relpath}: {failure.reason}",
                style="yellow",
                markup=False,
            )
    console.print(
        f"\nPlan: {len(plans)} resolved, {len(failures)} unresolved.",
        style="bold",
        markup=False,
    )
    if failures and not allow_unmatched:
        raise click.ClickException(
            "No changes made. Correct the unresolved metadata or rerun with "
            "--allow-unmatched to apply only the resolved books."
        )
    if not apply_changes:
        console.print(
            "Preview only; rerun with --apply after reviewing the plan.",
            style="cyan",
            markup=False,
        )
        return
    if not plans:
        raise click.ClickException("No resolved books are available to update.")

    cfg = config.load()
    console.print(
        "Validating UUIDs and applying metadata to local files and reMarkable…",
        style="bold cyan",
        markup=False,
    )
    try:
        with connected(cfg) as rm:
            result = apply_library_metadata(
                rm,
                root,
                plans,
                backup_files=backup_files,
                reporter=report,
            )
            if cfg.restart_xochitl:
                rm.restart_xochitl()
    except (BookMetadataError, OSError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    console.print(
        f"Updated {result.files_updated} books; renamed {result.files_renamed}. "
        f"Recovery backup: {result.backup_directory}",
        style="green",
        markup=False,
    )


@main.command(help="Remove byte-identical PDF/EPUB duplicates from a folder.")
@click.option(
    "--path",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Book folder to scan.",
)
@click.option("--dry-run", is_flag=True, help="Show duplicates without moving them.")
@click.option(
    "--delete",
    "permanent",
    is_flag=True,
    help="Permanently delete duplicates instead of moving them to recovery trash.",
)
def dedup(path: Path, dry_run: bool, permanent: bool) -> None:
    root = path.resolve()
    console = Console()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("Hashing {task.fields[file]}", markup=False),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    with progress:
        task = progress.add_task("dedup", total=None, file="candidate files…")

        def scanned(file: Path, index: int, total: int) -> None:
            progress.update(
                task,
                total=total,
                completed=index,
                file=file.relative_to(root).as_posix(),
            )

        groups = find_duplicate_groups(root, scanned)

    if not groups:
        console.print("No byte-identical PDF/EPUB duplicates found.", style="green")
        return

    for group in groups:
        console.print(f"Keep  {group.keeper.relative_to(root)}", style="green")
        for duplicate in group.duplicates:
            action = "Would remove" if dry_run else ("Delete" if permanent else "Move")
            console.print(f"{action:<12} {duplicate.relative_to(root)}", style="yellow")

    try:
        result = deduplicate(root, groups, dry_run=dry_run, permanent=permanent)
    except DeduplicationChangedError as error:
        raise click.ClickException(str(error)) from error
    size_mb = result.bytes_removed / 1024 / 1024
    if dry_run:
        console.print(
            f"Dry run: {result.files_removed} duplicates ({size_mb:.1f} MB) would be removed."
        )
    elif result.trash_directory:
        console.print(
            f"Removed {result.files_removed} duplicates ({size_mb:.1f} MB) from the "
            f"library. Recover them from {result.trash_directory}."
        )
    else:
        console.print(
            f"Permanently deleted {result.files_removed} duplicates ({size_mb:.1f} MB)."
        )


@main.command(help="Diagnose why a synced folder isn't appearing on the device.")
@click.option("--path", default=".", type=click.Path(exists=True, file_okay=False))
def doctor(path: str) -> None:
    root = Path(path).resolve()
    state = State.load(root)
    cfg = config.load()
    click.echo(f"Local:           {root}")
    click.echo(f"State root_uuid: {state.root_uuid or '(none)'}")
    click.echo(
        f"State tracking:  {len(state.folders)} folders, {len(state.files)} files"
    )
    if not state.root_uuid:
        click.echo("Nothing to inspect remotely — folder hasn't been synced yet.")
        return

    with connected(cfg) as rm:
        click.echo(f"\nRemote root dir: {cfg.remote_root}")
        _, out, _ = rm.exec(f"ls {cfg.remote_root} | wc -l")
        click.echo(f"  total entries: {out.strip()}")

        meta = rm.remote_path(f"{state.root_uuid}.metadata")
        click.echo(f"\nRoot collection metadata: {meta}")
        click.echo(f"  exists: {rm.exists(meta)}")
        if rm.exists(meta):
            click.echo("  contents:")
            for line in rm.get_text(meta).splitlines():
                click.echo(f"    {line}")

        click.echo("\nSample of tracked files (first 3):")
        for relpath, entry in list(state.files.items())[:3]:
            click.echo(f"  {relpath}  ({entry.uuid[:8]})")
            for ext in ("metadata", "content", entry.file_type):
                p = rm.remote_path(f"{entry.uuid}.{ext}")
                click.echo(f"    .{ext:<8}  exists={rm.exists(p)}")

        _, out, _ = rm.exec("systemctl is-active xochitl 2>&1")
        click.echo(f"\nxochitl service: {out.strip()}")

        _, out, _ = rm.exec(
            "grep -iE 'cloud|sync' /home/root/.config/remarkable/xochitl.conf "
            "2>/dev/null || echo '(no xochitl.conf or no cloud keys)'"
        )
        click.echo(
            "\nxochitl.conf cloud/sync keys:\n  " + out.strip().replace("\n", "\n  ")
        )

        _, out, _ = rm.exec(
            "cat /etc/version 2>/dev/null; cat /etc/remarkable/release 2>/dev/null"
        )
        click.echo("\nDevice firmware:\n  " + (out.strip() or "(unknown)"))


@main.command(help="Pretty tree listing of the current folder with sync status + tags.")
@click.option("--path", default=".", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--all", "show_all", is_flag=True, help="Also list untracked, unsupported files."
)
def ls(path: str, show_all: bool) -> None:
    from esbern.tui import render_ls

    render_ls(Path(path).resolve(), show_all=show_all)


@main.command(help="Show the sync state for the current directory.")
@click.option("--path", default=".", type=click.Path(exists=True, file_okay=False))
def status(path: str) -> None:
    root = Path(path).resolve()
    state = State.load(root)
    if not state.root_uuid:
        click.echo(f"{root} has not been synced yet.")
        return
    click.echo(f"Root: {root.name}  ({state.root_uuid})")
    click.echo(f"Folders tracked: {len(state.folders)}")
    click.echo(f"Files tracked:   {len(state.files)}")


@main.group(help="Inspect or edit the central tag store.")
def tag() -> None:
    pass


@tag.command("list", help="List all known tags and counts.")
def tag_list() -> None:
    store = TagStore.load()
    if not store.taxonomy:
        click.echo("(empty — nothing tagged yet)")
        return
    counts: dict[str, int] = {}
    for entry in store.files.values():
        for t in entry.tags:
            counts[t] = counts.get(t, 0) + 1
    width = max(len(t) for t in store.taxonomy)
    for t in sorted(store.taxonomy):
        click.echo(f"  {t:<{width}}  {counts.get(t, 0)}")
    click.echo(f"\nStored at {TAGS_PATH}")


@tag.command("show", help="Show tags for a specific file.")
@click.argument("relpath")
def tag_show(relpath: str) -> None:
    store = TagStore.load()
    store.scope_to(Path.cwd())
    entry = store.entry(relpath)
    if not entry:
        click.echo(f"No tags recorded for {relpath}.")
        return
    click.echo(json.dumps({"tags": entry.tags, "source": entry.source}, indent=2))


def _print_dry_run(root: Path) -> None:
    from esbern.sync import IGNORE_NAMES, SUPPORTED_EXTS, _walk_local  # noqa

    n_files = n_dirs = n_skip = 0
    for rel, is_dir in _walk_local(root):
        if is_dir:
            n_dirs += 1
            click.echo(f"  dir  {rel}/")
        elif rel.suffix.lower() in SUPPORTED_EXTS:
            n_files += 1
            click.echo(f"  file {rel}")
        else:
            n_skip += 1
            click.echo(f"  skip {rel}")
    click.echo(
        f"\nWould push {n_files} files, {n_dirs} folders "
        f"({n_skip} unsupported). Pull side requires a connection."
    )


if __name__ == "__main__":
    main()
