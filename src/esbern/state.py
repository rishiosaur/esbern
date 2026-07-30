"""Per-folder sync state.

Stored at <synced_dir>/.esbern/state.json. Maps each local file path
(relative to the sync root) to its on-device UUID plus enough info to
detect changes in either direction between syncs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath

STATE_DIRNAME = ".esbern"
STATE_FILENAME = "state.json"


def _validate_relpath(value: object, *, field_name: str) -> str:
    """Return a safe state key that cannot escape the local sync root."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid {field_name} path in sync state: {value!r}")
    path = Path(value)
    windows_path = PureWindowsPath(value)
    components = value.replace("\\", "/").split("/")
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or "\\" in value
        or "\x00" in value
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ValueError(f"Unsafe {field_name} path in sync state: {value!r}")
    return value


@dataclass
class FileEntry:
    uuid: str
    file_type: str  # "pdf" | "epub"
    size: int = 0  # local size at last sync
    mtime: float = 0.0  # local mtime at last sync
    remote_mtime: float = 0.0  # mtime of <uuid>.metadata (or <uuid>/) at last sync
    tags: list[str] = field(default_factory=list)


@dataclass
class State:
    root_uuid: str = ""
    folders: dict[str, str] = field(default_factory=dict)  # relpath → uuid
    folder_remote_mtime: dict[str, float] = field(default_factory=dict)
    files: dict[str, FileEntry] = field(default_factory=dict)  # relpath → entry

    @classmethod
    def load(cls, root: Path) -> State:
        path = root / STATE_DIRNAME / STATE_FILENAME
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        files: dict[str, FileEntry] = {}
        for k, v in raw.get("files", {}).items():
            relpath = _validate_relpath(k, field_name="file")
            files[relpath] = FileEntry(
                uuid=v["uuid"],
                file_type=v.get("file_type")
                or Path(relpath).suffix.lstrip(".").lower()
                or "pdf",
                size=v.get("size", 0),
                mtime=v.get("mtime", 0.0),
                remote_mtime=v.get("remote_mtime", 0.0),
                tags=list(v.get("tags", [])),
            )
        folders = {
            _validate_relpath(k, field_name="folder"): v
            for k, v in raw.get("folders", {}).items()
        }
        folder_remote_mtime = {
            _validate_relpath(k, field_name="folder mtime"): v
            for k, v in raw.get("folder_remote_mtime", {}).items()
        }
        return cls(
            root_uuid=raw.get("root_uuid", ""),
            folders=folders,
            folder_remote_mtime=folder_remote_mtime,
            files=files,
        )

    def save(self, root: Path) -> None:
        d = root / STATE_DIRNAME
        d.mkdir(exist_ok=True)
        path = d / STATE_FILENAME
        temporary = d / f"{STATE_FILENAME}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "root_uuid": self.root_uuid,
                    "folders": self.folders,
                    "folder_remote_mtime": self.folder_remote_mtime,
                    "files": {k: asdict(v) for k, v in self.files.items()},
                },
                indent=2,
            )
        )
        temporary.replace(path)

    def uuid_to_relpath(self) -> dict[str, str]:
        out = {uid: rel for rel, uid in self.folders.items()}
        for rel, entry in self.files.items():
            out[entry.uuid] = rel
        return out
