"""Builders + parsers for xochitl (.metadata / .content) JSON files.

xochitl is the reMarkable's document manager. Each item is a UUID with
sibling files:

  <uuid>.metadata   JSON: name, parent UUID, type, timestamps, tags
  <uuid>.content    JSON: fileType, pageCount, render settings
  <uuid>.pdf        the actual PDF (or .epub)
  <uuid>/           folder with .rm annotation pages, thumbnails

A "folder" is a metadata file with type=CollectionType and no payload.

Document-level tags live in .metadata's "tags" array; each tag is
{"name": str, "timestamp": ms}. xochitl shows these in the device UI.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _tag_objs(tags: Iterable[str]) -> list[dict]:
    ts = _now_ms()
    return [{"name": t, "timestamp": ts} for t in tags]


def collection_metadata(name: str, parent: str = "", tags: Iterable[str] = ()) -> str:
    return json.dumps({
        "deleted": False,
        "lastModified": _now_ms(),
        "metadatamodified": False,
        "modified": False,
        "parent": parent,
        "pinned": False,
        "synced": False,
        "tags": _tag_objs(tags),
        "type": "CollectionType",
        "version": 0,
        "visibleName": name,
    }, indent=4)


def document_metadata(name: str, parent: str = "", tags: Iterable[str] = ()) -> str:
    return json.dumps({
        "deleted": False,
        "lastModified": _now_ms(),
        "lastOpened": "0",
        "lastOpenedPage": 0,
        "metadatamodified": False,
        "modified": False,
        "parent": parent,
        "pinned": False,
        "synced": False,
        "tags": _tag_objs(tags),
        "type": "DocumentType",
        "version": 0,
        "visibleName": name,
    }, indent=4)


def update_document_metadata(
    metadata_json: str,
    *,
    name: str,
    parent: str,
    tags: Iterable[str] | None = None,
) -> str:
    """Update managed document fields while preserving device-owned fields."""
    data = json.loads(metadata_json)
    if not isinstance(data, dict):
        raise TypeError("document metadata must be a JSON object")
    data["visibleName"] = name
    data["parent"] = parent
    if tags is not None:
        data["tags"] = _tag_objs(tags)
    data["lastModified"] = _now_ms()
    return json.dumps(data, indent=4)


def document_content(file_type: str) -> str:
    return json.dumps({
        "coverPageNumber": 0,
        "dummyDocument": False,
        "extraMetadata": {},
        "fileType": file_type,
        "fontName": "",
        "lineHeight": -1,
        "margins": 100,
        "orientation": "portrait",
        "pageCount": 0,
        "pages": [],
        "textScale": 1,
        "transform": {},
    }, indent=4)


def empty_collection_content() -> str:
    return json.dumps({}, indent=4)


def restore_from_trash(metadata_json: str, desired_name: str | None = None) -> str | None:
    """If metadata has the item in trash/deleted (or its visible name has
    drifted from `desired_name`), return a corrected blob. Returns None
    when nothing needs to change.
    """
    data = json.loads(metadata_json)
    trashed = data.get("parent") == "trash" or data.get("deleted", False)
    name_drift = desired_name is not None and data.get("visibleName") != desired_name
    if not trashed and not name_drift:
        return None
    data["parent"] = "" if trashed else data.get("parent", "")
    data["deleted"] = False
    if name_drift:
        data["visibleName"] = desired_name
    data["lastModified"] = _now_ms()
    return json.dumps(data, indent=4)


def update_tags(metadata_json: str, tags: Iterable[str]) -> str:
    """Rewrite a metadata blob's tag list while preserving everything else."""
    data = json.loads(metadata_json)
    data["tags"] = _tag_objs(tags)
    data["lastModified"] = _now_ms()
    return json.dumps(data, indent=4)


def update_name(metadata_json: str, name: str) -> str:
    """Rewrite a visible name while preserving UUID-linked document state."""
    data = json.loads(metadata_json)
    data["visibleName"] = name
    data["lastModified"] = _now_ms()
    return json.dumps(data, indent=4)


def parse_metadata(metadata_json: str) -> dict:
    """Return a dict with normalized fields: name, parent, type, tags."""
    data = json.loads(metadata_json)
    raw_tags = data.get("tags") or []
    names = [t.get("name") if isinstance(t, dict) else str(t) for t in raw_tags]
    return {
        "name": data.get("visibleName", ""),
        "parent": data.get("parent", "") or "",
        "type": data.get("type", ""),
        "deleted": bool(data.get("deleted", False)),
        "tags": [n for n in names if n],
    }
