"""LLM-driven tag assignment + central tag store.

Tags live at ~/.config/esbern/tags.json:

    {
      "taxonomy": ["Research", "Programming", ...],
      "files": {
        "<local_relpath>": {"tags": ["Research", "Math"], "source": "claude"}
      }
    }

The taxonomy grows organically: each categorization call sees the current
taxonomy and either reuses existing tags or proposes new ones. File records
are keyed by resolved local path so identical relative paths in separate sync
roots do not share tags.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# pypdf is noisy about malformed cross-reference tables on scanned/legacy
# PDFs. We only need the text it can extract; the warnings clutter our
# sync output. Silence them.
logging.getLogger("pypdf").setLevel(logging.ERROR)

CONFIG_DIR = Path(
    os.environ.get("ESBERN_CONFIG_DIR", Path.home() / ".config" / "esbern")
)
TAGS_PATH = CONFIG_DIR / "tags.json"

MODEL = "claude-haiku-4-5-20251001"
MAX_TAGS = 3
MAX_TEXT_CHARS = 4000


@dataclass
class FileTags:
    tags: list[str]
    source: str = "claude"  # "claude" or "manual"


@dataclass
class TagStore:
    taxonomy: list[str] = field(default_factory=list)
    files: dict[str, FileTags] = field(default_factory=dict)
    root: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls) -> TagStore:
        if not TAGS_PATH.exists():
            return cls()
        raw = json.loads(TAGS_PATH.read_text())
        return cls(
            taxonomy=list(raw.get("taxonomy", [])),
            files={k: FileTags(**v) for k, v in raw.get("files", {}).items()},
        )

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "taxonomy": sorted(set(self.taxonomy)),
            "files": {k: asdict(v) for k, v in self.files.items()},
        }
        temporary = TAGS_PATH.with_suffix(f"{TAGS_PATH.suffix}.tmp")
        temporary.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(TAGS_PATH)

    def scope_to(self, root: Path) -> None:
        """Scope relative file records to one local sync root."""
        self.root = root.expanduser().resolve()

    def _key(self, relpath: str) -> str:
        if self.root is None:
            return relpath
        relative = Path(relpath)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"tag path must stay within the sync root: {relpath}")
        return str((self.root / relative).resolve())

    def entry(self, relpath: str) -> FileTags | None:
        key = self._key(relpath)
        entry = self.files.get(key)
        if entry is None and self.root is not None and relpath in self.files:
            # Migrate the old relative-path format when its owning root is
            # first used. Once migrated, another root cannot claim it.
            entry = self.files.pop(relpath)
            self.files[key] = entry
        return entry

    def get(self, relpath: str) -> list[str]:
        entry = self.entry(relpath)
        return list(entry.tags) if entry else []

    def set(self, relpath: str, tags: list[str], source: str = "claude") -> None:
        cleaned = [t.strip() for t in tags if t and t.strip()]
        key = self._key(relpath)
        if self.root is not None:
            self.files.pop(relpath, None)
        self.files[key] = FileTags(tags=cleaned[:MAX_TAGS], source=source)
        for t in cleaned:
            if t not in self.taxonomy:
                self.taxonomy.append(t)

    def remove(self, relpath: str) -> None:
        """Remove a scoped file record, including the legacy key if present."""
        self.files.pop(self._key(relpath), None)
        if self.root is not None:
            self.files.pop(relpath, None)

    def move(self, old_relpath: str, new_relpath: str) -> None:
        """Move a filename-keyed tag record without changing its provenance."""
        old_key = self._key(old_relpath)
        new_key = self._key(new_relpath)
        if old_key not in self.files and self.root is not None and old_relpath in self.files:
            self.files[old_key] = self.files.pop(old_relpath)
        if old_key == new_key or old_key not in self.files:
            return
        if new_key in self.files:
            raise ValueError(f"tag record already exists for {new_relpath}")
        self.files[new_key] = self.files.pop(old_key)


def extract_pdf_text(path: Path, max_pages: int = 2) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        chunks = []
        for page in reader.pages[:max_pages]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001, S112 - malformed pages are skipped
                continue
        return ("\n".join(chunks))[:MAX_TEXT_CHARS]
    except Exception:  # noqa: BLE001 - malformed PDFs are treated as textless
        return ""


def _build_prompt(filename: str, sample_text: str, taxonomy: list[str]) -> str:
    tax_list = (
        "\n".join(f"- {t}" for t in sorted(set(taxonomy))) if taxonomy else "(none yet)"
    )
    return f"""Categorize this document with 1–{MAX_TAGS} short tags.

Filename: {filename}

Existing tag taxonomy (reuse when reasonable, propose new tags only when none fit):
{tax_list}

Document excerpt:
\"\"\"
{sample_text or "(no extractable text — categorize based on filename alone)"}
\"\"\"

Rules:
- Each tag is 1–3 words, Title Case (e.g. "Machine Learning", "Memoir").
- Prefer broad, durable categories over narrow topics.
- Reuse existing taxonomy tags verbatim when they fit.

Respond with ONLY a JSON array of strings, like: ["Tag One", "Tag Two"]"""


def categorize(filename: str, sample_text: str, taxonomy: list[str]) -> list[str]:
    """Call Claude to categorize. Returns [] on any failure (no key, network, etc.)."""
    api_key = os.environ.get("ESBERN_ANT_API_KEY") or os.environ.get(
        "ANTHROPIC_API_KEY"
    )
    if not api_key:
        return []
    try:
        import anthropic
    except ImportError:
        return []
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": _build_prompt(filename, sample_text, taxonomy),
                }
            ],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        # Strip code fences if Claude wraps the JSON.
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            return []
        return [str(t).strip() for t in parsed if str(t).strip()][:MAX_TAGS]
    except Exception:  # noqa: BLE001 - tagging failures must not stop sync
        return []
