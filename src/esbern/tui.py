"""Rich-rendered listings for esbern.

`esbern ls` shows the synced folder as a tree, with one line per file
that combines status icon, name, tags, and size. The point is to make
"what's synced, what's pending, what's tagged" legible at a glance —
not to be a full interactive TUI.
"""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from esbern.state import FileEntry, State
from esbern.sync import IGNORE_NAMES, SUPPORTED_EXTS
from esbern.tags import TagStore

# (icon, rich style, label)
SYNCED       = ("●", "green",   "synced")
LOCAL_NEW    = ("+", "cyan",    "untracked")
LOCAL_AHEAD  = ("↑", "yellow",  "local newer")
LOCAL_GONE   = ("✗", "red",     "missing locally")
UNSUPPORTED  = ("·", "dim",     "unsupported")

TAG_PALETTE = [
    "bright_magenta", "bright_cyan", "bright_green", "bright_yellow",
    "bright_blue", "magenta", "cyan", "green", "yellow", "blue",
]


def _tag_color(taxonomy: list[str], tag: str) -> str:
    try:
        idx = taxonomy.index(tag)
    except ValueError:
        idx = abs(hash(tag))
    return TAG_PALETTE[idx % len(TAG_PALETTE)]


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f}{unit}"
    return f"{n:.1f}TB"


def _file_status(local_path: Path, entry: FileEntry | None,
                 supported: bool) -> tuple[str, str, str]:
    if entry is None:
        return UNSUPPORTED if not supported else LOCAL_NEW
    if not local_path.exists():
        return LOCAL_GONE
    st = local_path.stat()
    if st.st_size != entry.size or abs(st.st_mtime - entry.mtime) > 1.0:
        return LOCAL_AHEAD
    return SYNCED


def _file_line(name: str, status: tuple[str, str, str], size_bytes: int | None,
               tags: list[str], taxonomy: list[str]) -> Text:
    icon, style, _ = status
    line = Text()
    line.append(f"{icon} ", style=style)
    line.append(name)
    if size_bytes is not None:
        line.append(f"  {_format_size(size_bytes)}", style="dim")
    for t in tags:
        line.append("  ")
        line.append(f" {t} ", style=f"black on {_tag_color(taxonomy, t)}")
    return line


def _add_dir(tree: Tree, dirpath: Path, root: Path, state: State,
             store: TagStore, show_all: bool, counts: dict[str, int]) -> None:
    entries = sorted(dirpath.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    for entry in entries:
        if entry.name in IGNORE_NAMES or entry.is_symlink():
            continue
        if entry.is_dir():
            rel = entry.relative_to(root).as_posix()
            label = Text()
            label.append(entry.name + "/", style="bold")
            if rel in state.folders:
                label.append(f"  [{state.folders[rel][:8]}]", style="dim")
            sub = tree.add(label)
            _add_dir(sub, entry, root, state, store, show_all, counts)
            counts["dirs"] += 1
        elif entry.is_file():
            rel = entry.relative_to(root).as_posix()
            supported = entry.suffix.lower() in SUPPORTED_EXTS
            if not supported and not show_all:
                continue
            file_state = state.files.get(rel)
            status = _file_status(entry, file_state, supported)
            tags = (store.get(rel) or
                    (file_state.tags if file_state else []))
            size = entry.stat().st_size
            tree.add(_file_line(entry.name, status, size, tags, store.taxonomy))
            counts["files"] += 1
            counts[status[2]] = counts.get(status[2], 0) + 1


def render_ls(root: Path, show_all: bool = False) -> None:
    console = Console()
    state = State.load(root)
    store = TagStore.load()
    store.scope_to(root)

    header = Text()
    header.append(root.name, style="bold")
    if state.root_uuid:
        header.append(f"  [{state.root_uuid[:8]}]", style="dim")
    else:
        header.append("  (not yet synced)", style="dim italic")
    tree = Tree(header)

    counts = {"files": 0, "dirs": 0}
    _add_dir(tree, root, root, state, store, show_all, counts)

    # File entries in state but with no local file → list these so the
    # user can see what's still on the device-only side.
    orphaned = [r for r in state.files if not (root / r).exists()]
    if orphaned:
        ghost = tree.add(Text("(only on device — won't be deleted)", style="dim italic"))
        for r in sorted(orphaned):
            entry = state.files[r]
            tags = store.get(r) or entry.tags
            ghost.add(_file_line(r, LOCAL_GONE, None, tags, store.taxonomy))

    legend = Table.grid(padding=(0, 2))
    for icon, style, label in [SYNCED, LOCAL_NEW, LOCAL_AHEAD, LOCAL_GONE, UNSUPPORTED]:
        legend.add_row(Text(icon, style=style), Text(label, style="dim"))

    summary = Text()
    summary.append(f"{counts['files']} files · {counts['dirs']} folders")
    for label in ("synced", "untracked", "local newer", "missing locally"):
        if counts.get(label):
            summary.append(f"  ·  {counts[label]} {label}")
    if store.taxonomy:
        summary.append(f"\n{len(store.taxonomy)} known tags", style="dim")

    console.print(Panel(Group(tree, Text(""), summary, Text(""), legend),
                        title="esbern ls", box=box.ROUNDED, expand=False))
