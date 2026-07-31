"""Safe metadata enrichment and UUID-preserving library renames."""

from __future__ import annotations

import copy
import html
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from esbern import xochitl
from esbern.book_metadata import (
    BookMetadata,
    BookMetadataError,
    canonical_filename,
    epub_search_hints,
    read_epub_metadata,
    resolve_book_metadata,
    write_epub_metadata,
)
from esbern.remarkable import Remarkable
from esbern.state import STATE_DIRNAME, STATE_FILENAME, FileEntry, State
from esbern.sync import SUPPORTED_EXTS
from esbern.tags import TAGS_PATH, TagStore

LibraryReporter = Callable[[str, str], None]
METADATA_OVERRIDES_FILENAME = "metadata-overrides.json"
_OVERRIDE_FIELDS = {
    "google_id",
    "title",
    "authors",
    "published_date",
    "publisher",
    "description",
    "language",
    "categories",
    "isbn_10",
    "isbn_13",
}


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
class PendingMetadataResult:
    files_updated: int


@dataclass(frozen=True)
class _PreparedPlan:
    plan: LibraryMetadataPlan
    old_path: Path
    new_path: Path


@dataclass(frozen=True)
class _StagedPayload:
    prepared: _PreparedPlan
    local_original: Path
    rewritten_epub: Path | None


@dataclass
class _LocalMutation:
    staged: _StagedPayload
    payload_mutated: bool = False
    rename_attempted: bool = False
    renamed: bool = False


def _emit(reporter: LibraryReporter | None, action: str, detail: str) -> None:
    if reporter:
        reporter(action, detail)


def _repair_filename_text(value: str) -> str:
    value = value.replace("&_039_", "'").replace("&amp_", "&")
    value = html.unescape(value).replace("_", " ")
    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired.count("�") <= value.count("�"):
            value = repaired
            break
    return " ".join(value.split())


def _filename_hints(path: Path) -> tuple[str, tuple[str, ...], bool]:
    """Extract conservative author/title hints from common LibGen filenames."""
    stem = _repair_filename_text(path.stem)
    stem = re.sub(r"^(?:\[[^]]+\]\s*)+", "", stem)
    if " - " not in stem:
        return "", (), False

    author, title = (part.strip(" ,") for part in stem.split(" - ", 1))
    author = re.sub(r"^(?:\[[^]]+\]\s*)+", "", author).strip(" ,")
    author = re.sub(
        r"\s+by\s+.+?\((?:transl?|translator)[^)]*\).*$",
        "",
        author,
        flags=re.IGNORECASE,
    ).strip(" ,")
    author_tokens = set(re.findall(r"[^\W_]+", author.casefold()))
    reliable_author = bool(
        author
        and len(author_tokens) <= 12
        and "{" not in author
        and (
            author[:1].isupper()
            or "," in author
            or author.casefold().startswith("le guin")
        )
        and author.casefold() not in {"a", "an", "the"}
    )
    if not reliable_author:
        return "", (), False

    title = re.sub(r"\s+-\s+libgen\.li\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+\((?=[^)]*(?:18|19|20)\d{2})[^)]*\)\s*$", "", title)
    publisher_suffix = re.compile(
        r"\b(?:press|books?|publishing|publisher|classics?|library|gollancz|"
        r"harcourt|mariner|penguin|gateway|orbit|tor|bantam|knopf|fanucci|"
        r"hyperion|minotauro|epublibre)\b",
        re.IGNORECASE,
    )
    parenthetical = title.rfind(" (")
    if parenthetical >= 0 and publisher_suffix.search(title[parenthetical:]):
        title = title[:parenthetical]
    title = title.strip(" ,")

    # A few source filenames repeat the author before or after the real title.
    title_parts = [part.strip() for part in title.split(" - ")]
    if len(title_parts) > 1:
        first_terms = set(re.findall(r"[^\W_]+", title_parts[0].casefold()))
        last_terms = set(re.findall(r"[^\W_]+", title_parts[-1].casefold()))
        if first_terms and first_terms <= author_tokens:
            title = " - ".join(title_parts[1:])
        elif last_terms and last_terms <= author_tokens:
            title = " - ".join(title_parts[:-1])

    author_variants = [author]
    if author.count(",") == 1:
        family, given = (part.strip() for part in author.split(",", 1))
        author_variants.append(f"{given} {family}")
    for variant in author_variants:
        words = re.findall(r"[^\W_]+", variant)
        if not words:
            continue
        suffix = r"[\s,._-]+".join(re.escape(word) for word in words)
        shortened = re.sub(rf"[\s,._-]+{suffix}\s*$", "", title, flags=re.IGNORECASE)
        if shortened != title:
            title = shortened
            break

    if not title:
        return "", (), False
    return title, (author,), True


def _search_query(path: Path) -> tuple[str, str, tuple[str, ...], bool]:
    title, authors, filename_verified = _filename_hints(path)
    if path.suffix.lower() == ".epub":
        embedded_title, embedded_authors = epub_search_hints(path)
        if title and embedded_title:
            expected_terms = set(re.findall(r"[^\W_]+", title.casefold()))
            embedded_terms = set(re.findall(r"[^\W_]+", embedded_title.casefold()))
            coverage = (
                len(expected_terms & embedded_terms) / len(expected_terms)
                if expected_terms
                else 0.0
            )
            if coverage >= 0.6:
                title, authors = embedded_title, embedded_authors or authors
        elif not title:
            title, authors = embedded_title, embedded_authors
    query = " ".join((title, *authors)).strip()
    if not query:
        query = _repair_filename_text(path.stem)
    return query, title, authors, filename_verified


def _verified_authors(
    metadata: BookMetadata, expected_authors: tuple[str, ...], strict: bool
) -> BookMetadata:
    """Discard catalog contributors contradicted by a clear filename author."""
    if not strict or not expected_authors:
        return metadata
    expected_terms = set(re.findall(r"[^\W_]+", " ".join(expected_authors).casefold()))
    authors = tuple(
        author
        for author in metadata.authors
        if (
            (terms := set(re.findall(r"[^\W_]+", author.casefold())))
            and len(terms & expected_terms) / len(terms) >= 0.5
        )
    )
    return replace(metadata, authors=authors) if authors else metadata


def _override_text(value: object, *, field: str, relpath: str) -> str:
    if not isinstance(value, str):
        raise BookMetadataError(f"{relpath}: override {field} must be text")
    return " ".join(value.split())


def _override_strings(value: object, *, field: str, relpath: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BookMetadataError(f"{relpath}: override {field} must be a list")
    values = tuple(_override_text(item, field=field, relpath=relpath) for item in value)
    if any(not item for item in values):
        raise BookMetadataError(f"{relpath}: override {field} contains an empty value")
    return values


def _metadata_from_override(relpath: str, raw: object) -> BookMetadata:
    if not isinstance(raw, dict):
        raise BookMetadataError(f"{relpath}: metadata override must be an object")
    unknown = set(raw) - _OVERRIDE_FIELDS
    if unknown:
        raise BookMetadataError(
            f"{relpath}: unknown metadata override field(s): "
            f"{', '.join(sorted(unknown))}"
        )
    for required in ("title", "authors", "published_date"):
        if required not in raw:
            raise BookMetadataError(
                f"{relpath}: metadata override is missing {required}"
            )
    book = BookMetadata(
        google_id=_override_text(
            raw.get("google_id", ""), field="google_id", relpath=relpath
        ),
        title=_override_text(raw["title"], field="title", relpath=relpath),
        authors=_override_strings(raw["authors"], field="authors", relpath=relpath),
        published_date=_override_text(
            raw["published_date"], field="published_date", relpath=relpath
        ),
        publisher=_override_text(
            raw.get("publisher", ""), field="publisher", relpath=relpath
        ),
        description=_override_text(
            raw.get("description", ""), field="description", relpath=relpath
        ),
        language=_override_text(
            raw.get("language", ""), field="language", relpath=relpath
        ),
        categories=_override_strings(
            raw.get("categories", []), field="categories", relpath=relpath
        ),
        isbn_10=_override_strings(
            raw.get("isbn_10", []), field="isbn_10", relpath=relpath
        ),
        isbn_13=_override_strings(
            raw.get("isbn_13", []), field="isbn_13", relpath=relpath
        ),
    )
    if not book.title or not book.authors or not book.year:
        raise BookMetadataError(
            f"{relpath}: override requires a title, at least one author, and a year"
        )
    return book


def _load_metadata_overrides(local_root: Path) -> dict[str, BookMetadata]:
    path = local_root / STATE_DIRNAME / METADATA_OVERRIDES_FILENAME
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise BookMetadataError(f"metadata overrides must be a regular file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BookMetadataError(
            f"could not read metadata overrides: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise BookMetadataError("metadata overrides must be a JSON object")
    return {
        relpath: _metadata_from_override(relpath, value)
        for relpath, value in raw.items()
        if isinstance(relpath, str)
    }


def _save_metadata_overrides(
    local_root: Path, overrides: dict[str, BookMetadata]
) -> None:
    path = local_root / STATE_DIRNAME / METADATA_OVERRIDES_FILENAME
    payload: dict[str, dict] = {}
    for relpath, metadata in overrides.items():
        values = asdict(metadata)
        values["authors"] = list(metadata.authors)
        values["categories"] = list(metadata.categories)
        values["isbn_10"] = list(metadata.isbn_10)
        values["isbn_13"] = list(metadata.isbn_13)
        payload[relpath] = {
            key: value for key, value in values.items() if value not in ("", (), [])
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def plan_library_metadata(
    local_root: Path,
    *,
    api_key: str | None,
    reporter: LibraryReporter | None = None,
) -> tuple[list[LibraryMetadataPlan], list[LibraryMetadataFailure]]:
    """Resolve every tracked local book without changing disk or device state."""
    local_root = _absolute_root(local_root)
    state = State.load(local_root)
    overrides = _load_metadata_overrides(local_root)
    unknown_overrides = set(overrides) - set(state.files)
    if unknown_overrides:
        raise BookMetadataError(
            "metadata override(s) do not match tracked files: "
            + ", ".join(sorted(unknown_overrides, key=str.casefold))
        )
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
            metadata = overrides.get(relpath)
            if metadata is None:
                query, expected_title, expected_authors, strict_authors = _search_query(
                    path
                )
                metadata, metadata_source = resolve_book_metadata(
                    path,
                    query,
                    api_key=api_key,
                    expected_title=expected_title,
                    expected_authors=expected_authors,
                )
                if metadata_source == "libgen":
                    _emit(reporter, "fallback", relpath)
                metadata = _verified_authors(metadata, expected_authors, strict_authors)
            else:
                _emit(reporter, "override", relpath)
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


def load_library_metadata_plan(
    local_root: Path,
    backup_or_plan: Path,
    *,
    reporter: LibraryReporter | None = None,
) -> list[LibraryMetadataPlan]:
    """Load a complete saved plan, including legacy staged-EPUB backups."""
    local_root = _absolute_root(local_root)
    state = State.load(local_root)
    overrides = _load_metadata_overrides(local_root)
    requested = backup_or_plan.expanduser().absolute()
    plan_path = requested / "plan.json" if requested.is_dir() else requested
    backup = plan_path.parent
    try:
        raw_plans = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BookMetadataError(
            f"could not read saved metadata plan: {error}"
        ) from error
    if not isinstance(raw_plans, list):
        raise BookMetadataError("saved metadata plan must be a JSON array")

    plans: list[LibraryMetadataPlan] = []
    seen: set[str] = set()
    canonical_pattern = re.compile(
        r"^(?P<authors>.+) - (?P<title>.+) \((?P<year>1\d{3}|20\d{2})\)$"
    )
    for index, raw in enumerate(raw_plans, start=1):
        if not isinstance(raw, dict) or not isinstance(raw.get("old_relpath"), str):
            raise BookMetadataError(f"saved metadata plan entry {index} is invalid")
        old_relpath = raw["old_relpath"]
        if old_relpath in seen or old_relpath not in state.files:
            raise BookMetadataError(
                f"saved metadata plan does not match tracked file: {old_relpath}"
            )
        seen.add(old_relpath)
        source = _safe_local_path(local_root, old_relpath, role="source")

        raw_metadata = raw.get("metadata")
        if raw_metadata is not None:
            metadata = _metadata_from_override(old_relpath, raw_metadata)
        elif old_relpath in overrides:
            metadata = overrides[old_relpath]
        elif source.suffix.lower() == ".epub":
            metadata = read_epub_metadata(backup / ".prepared" / f"{index}.epub")
        else:
            saved_destination = raw.get("new_relpath")
            if not isinstance(saved_destination, str):
                raise BookMetadataError(
                    f"saved metadata plan entry {index} has no destination"
                )
            match = canonical_pattern.fullmatch(Path(saved_destination).stem)
            if match is None:
                raise BookMetadataError(
                    f"saved metadata plan entry {index} has no recoverable metadata"
                )
            metadata = BookMetadata(
                google_id=str(raw.get("google_id") or ""),
                title=match.group("title"),
                authors=tuple(match.group("authors").split(", ")),
                published_date=match.group("year"),
            )

        new_name = canonical_filename(metadata, source.suffix)
        new_relpath = (Path(old_relpath).parent / new_name).as_posix()
        plans.append(LibraryMetadataPlan(old_relpath, new_relpath, metadata))
        _emit(
            reporter,
            "resumed",
            f"[{index}/{len(raw_plans)}] {old_relpath} -> {new_relpath}",
        )

    missing = set(state.files) - seen
    if missing:
        raise BookMetadataError(
            "saved metadata plan is incomplete; missing: "
            + ", ".join(sorted(missing, key=str.casefold))
        )
    return plans


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

    def parsed(uid: str) -> dict | None:
        if uid in parsed_by_uuid:
            return parsed_by_uuid[uid]
        raw = metadata_by_uuid.get(uid)
        if raw is None:
            return None
        try:
            node = xochitl.parse_metadata(raw)
        except (TypeError, ValueError) as error:
            raise BookMetadataError(
                f"reMarkable metadata for {uid} is invalid"
            ) from error
        parsed_by_uuid[uid] = node
        return node

    root = parsed(state.root_uuid)
    if root is None:
        raise BookMetadataError("saved reMarkable root UUID is missing")
    if root["type"] != "CollectionType" or root["parent"] or root["deleted"]:
        raise BookMetadataError("saved reMarkable root is not an active collection")

    def is_active_descendant(uid: str) -> bool:
        seen: set[str] = set()
        current = uid
        while current and current not in seen:
            seen.add(current)
            node = parsed(current)
            if node is None or node["deleted"] or node["parent"] == "trash":
                return False
            if node["parent"] == state.root_uuid:
                return True
            current = node["parent"]
        return False

    originals: dict[str, str] = {}
    for plan in plans:
        entry = state.files[plan.old_relpath]
        node = parsed(entry.uuid)
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


def _restore_overrides(
    local_root: Path,
    overrides_before: dict[str, BookMetadata],
    overrides_existed: bool,
) -> None:
    overrides_path = local_root / STATE_DIRNAME / METADATA_OVERRIDES_FILENAME
    if overrides_existed:
        _save_metadata_overrides(local_root, overrides_before)
    else:
        overrides_path.unlink(missing_ok=True)


def _rollback_local_batch(
    local_root: Path,
    mutations: list[_LocalMutation],
    state_before: State,
    store_before: TagStore,
    overrides_before: dict[str, BookMetadata],
    overrides_existed: bool,
) -> list[str]:
    """Best-effort restore the real local library after a batch failure."""
    errors: list[str] = []
    for mutation in reversed(mutations):
        prepared = mutation.staged.prepared
        old_path = prepared.old_path
        new_path = prepared.new_path
        try:
            if mutation.renamed:
                if new_path.is_symlink() or not new_path.is_file():
                    raise OSError(f"renamed payload is missing or unsafe: {new_path}")
                if old_path.exists() or old_path.is_symlink():
                    raise OSError(
                        f"original filename was concurrently occupied: {old_path}"
                    )
                shutil.copy2(mutation.staged.local_original, new_path)
                _rename_without_clobber(new_path, old_path)
            else:
                if (
                    mutation.rename_attempted
                    and new_path.exists()
                    and old_path.exists()
                    and new_path.samefile(old_path)
                ):
                    new_path.unlink()
                if mutation.payload_mutated:
                    if old_path.is_symlink() or not old_path.is_file():
                        raise OSError(
                            f"original payload is missing or unsafe: {old_path}"
                        )
                    shutil.copy2(mutation.staged.local_original, old_path)
        except Exception as error:  # noqa: BLE001 - collect every rollback failure
            errors.append(
                f"{prepared.plan.old_relpath}: local payload restore failed: {error}"
            )

    try:
        state_before.save(local_root)
    except Exception as error:  # noqa: BLE001 - collect every rollback failure
        errors.append(f"state restore failed: {error}")
    try:
        store_before.save()
    except Exception as error:  # noqa: BLE001 - collect every rollback failure
        errors.append(f"tag restore failed: {error}")
    try:
        _restore_overrides(local_root, overrides_before, overrides_existed)
    except Exception as error:  # noqa: BLE001 - collect every rollback failure
        errors.append(f"metadata override restore failed: {error}")
    return errors


def _rollback_remote_item(
    rm: Remarkable,
    local_root: Path,
    entry: FileEntry,
    local_original: Path,
    state_before: State,
    *,
    remote_payload_attempted: bool,
    remote_metadata_attempted: bool,
    remote_metadata_before: str,
) -> list[str]:
    """Best-effort restore one device document without undoing local names."""
    errors: list[str] = []
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
        state_before.save(local_root)
    except Exception as error:  # noqa: BLE001 - collect every rollback failure
        errors.append(f"state restore failed: {error}")
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


def _stage_local_payloads(
    prepared_plans: list[_PreparedPlan],
    backup: Path,
    *,
    backup_files: bool,
    workers: int,
    reporter: LibraryReporter | None,
) -> list[_StagedPayload]:
    """Back up and rewrite every EPUB before the first device mutation."""
    rollback_directory = backup / ".rollback"
    rewritten_directory = backup / ".prepared"

    def stage(index: int, prepared: _PreparedPlan) -> _StagedPayload:
        plan = prepared.plan
        if backup_files:
            local_original = backup / "files" / plan.old_relpath
        else:
            local_original = rollback_directory / f"{index}{prepared.old_path.suffix}"
        local_original.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(prepared.old_path, local_original)

        rewritten: Path | None = None
        if prepared.old_path.suffix.lower() == ".epub":
            rewritten_directory.mkdir(parents=True, exist_ok=True)
            rewritten = rewritten_directory / f"{index}.epub"
            shutil.copy2(prepared.old_path, rewritten)
            write_epub_metadata(rewritten, plan.metadata)
        return _StagedPayload(prepared, local_original, rewritten)

    staged_by_index: dict[int, _StagedPayload] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(stage, index, prepared): (index, prepared)
            for index, prepared in enumerate(prepared_plans, start=1)
        }
        for future in as_completed(futures):
            index, prepared = futures[future]
            try:
                staged_by_index[index] = future.result()
            except Exception as error:
                raise BookMetadataError(
                    f"{prepared.plan.old_relpath}: local metadata preparation "
                    f"failed: {error}"
                ) from error
            _emit(
                reporter,
                "prepared",
                f"[{len(staged_by_index)}/{len(prepared_plans)}] "
                f"{prepared.plan.new_relpath}",
            )
    return [staged_by_index[index] for index in sorted(staged_by_index)]


def _commit_local_batch(
    local_root: Path,
    staged_payloads: list[_StagedPayload],
    state: State,
    store: TagStore,
    overrides: dict[str, BookMetadata],
    *,
    overrides_existed: bool,
    reporter: LibraryReporter | None,
) -> int:
    """Commit every real local payload/name before the first device write."""
    state_before = copy.deepcopy(state)
    store_before = copy.deepcopy(store)
    overrides_before = copy.deepcopy(overrides)
    mutations: list[_LocalMutation] = []
    overrides_mutated = False
    renamed = 0

    try:
        for index, staged in enumerate(staged_payloads, start=1):
            prepared = staged.prepared
            plan = prepared.plan
            mutation = _LocalMutation(staged)
            mutations.append(mutation)

            if staged.rewritten_epub is not None:
                mutation.payload_mutated = True
                os.replace(staged.rewritten_epub, prepared.old_path)

            if prepared.new_path != prepared.old_path:
                mutation.rename_attempted = True
                _rename_without_clobber(prepared.old_path, prepared.new_path)
                mutation.renamed = True
                renamed += 1

            entry = state.files.pop(plan.old_relpath)
            # A pending sentinel ensures an ordinary sync can finish pushing a
            # locally committed batch if normalization is interrupted later.
            entry.size = -1
            entry.mtime = 0.0
            state.files[plan.new_relpath] = entry
            store.move(plan.old_relpath, plan.new_relpath)
            if plan.old_relpath in overrides and plan.new_relpath != plan.old_relpath:
                overrides[plan.new_relpath] = overrides.pop(plan.old_relpath)
                overrides_mutated = True
            _emit(
                reporter,
                "local",
                f"[{index}/{len(staged_payloads)}] {plan.new_relpath}",
            )

        state.save(local_root)
        store.save()
        if overrides_mutated:
            _save_metadata_overrides(local_root, overrides)
    except BaseException as error:
        rollback_errors = _rollback_local_batch(
            local_root,
            mutations,
            state_before,
            store_before,
            overrides_before,
            overrides_existed,
        )
        if not isinstance(error, Exception):
            raise
        raise BookMetadataError(
            _metadata_error("local library", error, rollback_errors)
        ) from error
    return renamed


def apply_library_metadata(
    rm: Remarkable,
    local_root: Path,
    plans: list[LibraryMetadataPlan],
    *,
    backup_files: bool = True,
    workers: int = 4,
    reporter: LibraryReporter | None = None,
) -> LibraryMetadataResult:
    """Apply a fully resolved plan, checkpointing each preserved UUID."""
    local_root = _absolute_root(local_root)
    state = State.load(local_root)
    store = TagStore.load()
    store.scope_to(local_root)
    overrides = _load_metadata_overrides(local_root)
    if not 1 <= workers <= 32:
        raise BookMetadataError("metadata workers must be between 1 and 32")
    prepared_plans = _preflight_local_and_tags(local_root, state, store, plans)
    originals = _validate_remote(rm, state, plans)

    source_bytes = sum(item.old_path.stat().st_size for item in prepared_plans)
    staged_epub_bytes = sum(
        item.old_path.stat().st_size
        for item in prepared_plans
        if item.old_path.suffix.lower() == ".epub"
    )
    available = shutil.disk_usage(local_root).free
    if available < source_bytes + staged_epub_bytes + 100 * 1024 * 1024:
        raise BookMetadataError(
            "not enough free disk space for metadata preparation and rollback copies"
        )

    backup = _backup_directory(local_root)
    state_path = local_root / STATE_DIRNAME / STATE_FILENAME
    if state_path.exists():
        shutil.copy2(state_path, backup / STATE_FILENAME)
    if TAGS_PATH.exists():
        shutil.copy2(TAGS_PATH, backup / "tags.json")
    overrides_path = local_root / STATE_DIRNAME / METADATA_OVERRIDES_FILENAME
    overrides_existed = overrides_path.exists()
    if overrides_existed:
        shutil.copy2(overrides_path, backup / METADATA_OVERRIDES_FILENAME)
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
                    "metadata": asdict(plan.metadata),
                }
                for plan in plans
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    staged_payloads = _stage_local_payloads(
        prepared_plans,
        backup,
        backup_files=backup_files,
        workers=workers,
        reporter=reporter,
    )

    renamed = _commit_local_batch(
        local_root,
        staged_payloads,
        state,
        store,
        overrides,
        overrides_existed=overrides_existed,
        reporter=reporter,
    )

    rollback_directory = backup / ".rollback"
    for index, staged in enumerate(staged_payloads, start=1):
        prepared = staged.prepared
        plan = prepared.plan
        new_path = prepared.new_path
        local_original = staged.local_original
        entry: FileEntry = state.files[plan.new_relpath]
        _emit(
            reporter,
            "updating",
            f"[{index}/{len(prepared_plans)}] {plan.old_relpath}",
        )

        state_before = copy.deepcopy(state)
        remote_payload_attempted = False
        remote_metadata_attempted = False
        remote_metadata_before = originals[entry.uuid]

        try:
            if entry.file_type == "epub":
                remote_payload_attempted = True
                rm.put_file(new_path, rm.remote_path(f"{entry.uuid}.epub"))

            metadata_path = rm.remote_path(f"{entry.uuid}.metadata")
            remote_metadata_before = rm.get_text(metadata_path)
            parsed = xochitl.parse_metadata(remote_metadata_before)
            desired_name = Path(plan.new_relpath).stem
            if parsed["name"] != desired_name:
                remote_metadata_attempted = True
                rm.put_text(
                    metadata_path,
                    xochitl.update_name(remote_metadata_before, desired_name),
                )

            stat = new_path.stat()
            entry.size = stat.st_size
            entry.mtime = stat.st_mtime
            entry.remote_mtime = max(
                rm.stat_mtime(metadata_path), rm.annotation_dir_mtime(entry.uuid)
            )
            state.save(local_root)
        except BaseException as error:
            rollback_errors: list[str] = []
            if remote_payload_attempted or remote_metadata_attempted:
                rollback_errors = _rollback_remote_item(
                    rm,
                    local_root,
                    entry,
                    local_original,
                    state_before,
                    remote_payload_attempted=remote_payload_attempted,
                    remote_metadata_attempted=remote_metadata_attempted,
                    remote_metadata_before=remote_metadata_before,
                )
                state = state_before
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
        _emit(reporter, "updated", plan.new_relpath)

    if rollback_directory.exists():
        try:
            rollback_directory.rmdir()
        except OSError:
            pass
    rewritten_directory = backup / ".prepared"
    if rewritten_directory.exists():
        try:
            rewritten_directory.rmdir()
        except OSError:
            pass

    return LibraryMetadataResult(
        files_updated=len(prepared_plans),
        files_renamed=renamed,
        backup_directory=backup,
    )


def resume_pending_library_metadata(
    rm: Remarkable,
    local_root: Path,
    *,
    reporter: LibraryReporter | None = None,
) -> PendingMetadataResult:
    """Finish a locally committed batch using atomic device replacements."""
    local_root = _absolute_root(local_root)
    state = State.load(local_root)
    pending = [
        (relpath, entry)
        for relpath, entry in state.files.items()
        if entry.size == -1 and entry.mtime == 0.0
    ]
    pending.sort(key=lambda item: item[0].casefold())
    if not pending:
        return PendingMetadataResult(files_updated=0)

    validation_plans = [
        LibraryMetadataPlan(
            relpath,
            relpath,
            BookMetadata(
                google_id="",
                title=Path(relpath).stem,
                authors=("Pending",),
                published_date="2000",
            ),
        )
        for relpath, _ in pending
    ]
    _validate_remote(rm, state, validation_plans)

    for index, (relpath, entry) in enumerate(pending, start=1):
        path = _safe_local_path(local_root, relpath, role="source")
        if path.is_symlink() or not path.is_file():
            raise BookMetadataError(f"{relpath}: pending local file is missing")
        if path.suffix.lower() != f".{entry.file_type}":
            raise BookMetadataError(
                f"{relpath}: pending file type does not match state"
            )
        _emit(reporter, "updating", f"[{index}/{len(pending)}] {relpath}")
        try:
            if entry.file_type == "epub":
                rm.put_file(path, rm.remote_path(f"{entry.uuid}.epub"))

            metadata_path = rm.remote_path(f"{entry.uuid}.metadata")
            metadata_before = rm.get_text(metadata_path)
            parsed = xochitl.parse_metadata(metadata_before)
            if parsed["name"] != path.stem:
                rm.put_text(
                    metadata_path,
                    xochitl.update_name(metadata_before, path.stem),
                )

            stat = path.stat()
            entry.size = stat.st_size
            entry.mtime = stat.st_mtime
            entry.remote_mtime = max(
                rm.stat_mtime(metadata_path), rm.annotation_dir_mtime(entry.uuid)
            )
            state.save(local_root)
        except Exception as error:
            raise BookMetadataError(
                f"{relpath}: pending device update failed: "
                f"{str(error) or type(error).__name__}"
            ) from error
        _emit(reporter, "updated", relpath)

    return PendingMetadataResult(files_updated=len(pending))
