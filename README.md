# esbern

Two-way sync between a local folder and a reMarkable Paper Pro, with
LLM-driven tags that show up on both sides.

```sh
cd ~/Documents/papers
esbern sync
```

- New local PDFs/EPUBs get pushed to the device.
- New (or annotated) documents on the device get pulled into the folder.
- Each new file gets 1–3 tags from Claude Haiku. The tags are written
  into xochitl metadata so they appear in the reMarkable UI, *and*
  stored centrally at `~/.config/esbern/tags.json`.
- **`esbern sync` never deletes anything.** If a file vanishes from one side,
  it stays on the other.

## How it works

`esbern` talks to the reMarkable over SSH/SFTP. Each local folder gets
its own root Collection on the device (named after the folder). Sync
state lives at `<folder>/.esbern/state.json` and maps every local
relpath to its on-device UUID, plus enough metadata (local mtime, remote
mtime, tags) to detect changes in either direction.

On the first sync, Esbern adopts a unique top-level reMarkable folder
whose name matches the local folder (case-insensitively). If more than
one folder matches, it stops and asks you to resolve the duplicate
instead of guessing or creating another folder.

**Push:** validate the linked root, walk the local tree, and trust the last
local checkpoint for unchanged books. Only new or locally modified books touch
the device; use a full sync when you need remote deletion or annotation changes
reconciled.

**Pull:** read the parent-link headers needed to reconstruct reMarkable's flat
UUID hierarchy, then inventory and process only descendants of the active
top-level Collection matching the local folder name. For Documents already
seen, re-download if the device-side `.metadata` or annotation directory has a
newer mtime. Other top-level folders and trash entries are never synchronized.

**Conflicts:** if both sides have changed since the last sync, the
device wins (that's where annotations happen). The CLI prints a notice.

**Tags:** when a brand-new file appears (on either side), `esbern`
extracts the first ~2 pages of text and asks Claude to pick 1–3 tags
from the current taxonomy (or propose new ones). Tags are written to
both the central store and the xochitl metadata so they show up on
the device. No `ESBERN_ANT_API_KEY` → tagging is silently skipped.

## Install

```sh
uv sync
uv tool install --editable .   # puts `esbern` on your PATH
uv run pytest                  # verify the checkout
uv run ruff check .
```

Requires Python 3.10+.

## Setup

1. Plug the reMarkable in via USB (or put it on the same Wi-Fi network).
2. On the device: Settings → Help → Copyrights and licenses. The SSH
   password is at the top.
3. `esbern init`, paste the password.
4. `esbern ping` to verify.
5. `export ESBERN_ANT_API_KEY=…` if you want LLM tagging.
6. Optionally put `GOOGLE_BOOKS_API_KEY=…` in the project `.env` (ignored by
   Git), or export it in the shell, for richer catalog metadata. Without a
   working key, Esbern cleans the LibGen filename and embedded EPUB metadata
   locally.

For Wi-Fi sync, pass `--host <device-ip>` to `esbern init`.
The first successful SSH connection pins the device host key in the private
config file; later key changes are rejected. Rerun `esbern init` to trust a
replacement device only after verifying the connection.

## Commands

| Command            | What it does                                       |
| ------------------ | -------------------------------------------------- |
| `esbern init`      | Save SSH connection details                        |
| `esbern ping`      | Verify the SSH connection                          |
| `esbern get …`     | Download a book and push the new file to reMarkable |
| `esbern serve`     | Serve the cover grid and library HTTP API           |
| `esbern normalize` | Preview/apply UUID-preserving library cleanup       |
| `esbern dedup`     | Move byte-identical local duplicates to recovery   |
| `esbern sync`      | Two-way sync (push + pull) for the current folder  |
| `esbern push`      | Push local books to reMarkable without pulling      |
| `esbern pull`      | Pull from the matching reMarkable folder only      |
| `esbern status`    | Show sync state for the current folder             |
| `esbern ls`        | Show a tree of local sync and tag status            |
| `esbern doctor`    | Diagnose the configured device and sync root        |
| `esbern tag list`  | List all known tags and counts                     |
| `esbern tag show`  | Show tags for a specific file (by relpath)         |

`esbern sync --dry-run` shows what the push side *would* upload without
connecting.

## Run the library server

Start the UV-managed Python server from the library root. It listens on every
interface so it is reachable through Tailscale by default:

```sh
cd ~/esbern
ESBERN_INBOX_DIR=Books uv run esbern serve --path ~/reading
# http://server:3000       image-only cover grid
# http://server:3000/docs  generated API reference
```

Use `--host 127.0.0.1` to keep it local or `--port 8080` to choose another
port. The root page recursively finds every PDF and EPUB in the library and
renders one cover image per file. EPUB cover art and first-page PDF images are
used when available; otherwise Esbern generates a deterministic title cover.

The JSON API exposes the same downloader and transfer engine as the CLI. A
successful single or bulk download is installed locally first, then only the
newly downloaded files are pushed to reMarkable. It does not pull or scan the
full device library. Explicit push, pull, and two-way sync jobs remain available
for maintenance. When the library root contains existing independently synced
folders, each operation preserves those scopes without creating a duplicate
top-level collection. New downloads default to a synced folder named `Books`;
set `ESBERN_INBOX_DIR` to a relative folder name to choose a different inbox:

```sh
# Catalog
curl http://server:3000/api/books

# Search title, author, year, folder, path, or format
curl --get http://server:3000/api/books/search \
  --data-urlencode 'q=ursula le guin dispossessed'

# Queue one book for phone/assistant clients and poll the returned job URL
curl -X POST http://server:3000/api/jobs/books \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"query":"The Left Hand of Darkness Ursula Le Guin"}'

# One book
curl -X POST http://server:3000/api/books \
  -H 'Content-Type: application/json' \
  -d '{"query":"The Left Hand of Darkness Ursula Le Guin"}'

# Raw UTF-8 text: one query per non-empty line
curl -X POST 'http://server:3000/api/books/bulk?jobs=4' \
  -H 'Content-Type: text/plain' \
  --data-binary @books.txt

# Push server books to reMarkable without pulling
curl -X POST http://server:3000/api/jobs/push \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"workers":4}'

# Pull reMarkable changes to the server without pushing
curl -X POST http://server:3000/api/jobs/pull \
  -H 'Authorization: Bearer <token>'

# Reconcile all nested folders with the reMarkable
curl -X POST http://server:3000/api/sync \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Mutation routes are public unless `ESBERN_API_TOKEN` is set. When it is set,
send `Authorization: Bearer <token>` with download and transfer requests. Set
`ESBERN_CORS_ORIGIN` to restrict cross-origin browser clients; it defaults to
`*`. Put the server behind a reverse proxy if it is exposed beyond a trusted
Tailscale network.

Phone-friendly ChatGPT and Claude setup lives in
[`skills/manage-esbern-library`](skills/manage-esbern-library). ChatGPT uses
the hosted OpenAPI action schema; Claude uses the hosted MCP connector. Both
can list and search the public catalog, queue authenticated background book
installs, launch explicit push, pull, or full-sync jobs, and check job status.
Book installs automatically push only their newly downloaded files, so
assistants must not queue a separate transfer afterward. Claude's `add_book`,
`normalize_book`, `push_library`, `pull_library`, and `sync_library` tools keep
their MCP requests open and stream persisted progress; the underlying jobs
continue if a request is interrupted. `normalize_book` targets one exact catalog
ID. It preserves the reMarkable UUID of a tracked book, while an untracked local
book is cleaned without being pushed. ChatGPT queues its supported durable jobs
and polls their status separately.

Uploads use four parallel SSH workers by default. Each worker independently
classifies and transfers a book, while completed books are checkpointed one at
a time so an interrupted run resumes safely. Use `esbern sync --workers N`
(1–8), or set `ESBERN_SYNC_WORKERS`, to tune concurrency. If a run is
interrupted after changing the device, Esbern still refreshes the reMarkable
document service so completed uploads appear in the UI.

Normal `push`, `sync`, and `pull` runs are intentionally verbose. They show connection
timing, every local entry examined, every book and folder inside the selected
reMarkable subtree, live byte and speed progress for each transfer, tag
classification, state saving, restart status, and a final summary. Unrelated
top-level reMarkable folders are not listed or synchronized.

Two-way sync is device-first: it pulls from the reMarkable, removes local
byte-identical duplicates, and only then pushes genuinely local books. If an
untracked local book has the same folder and title as a reMarkable book, the
reMarkable copy wins. The displaced local file is kept under
`.esbern/sync-collision-trash/`; content duplicates go to
`.esbern/dedup-trash/`. Neither is permanently deleted.

Items in the reMarkable trash are never pulled or used as the sync root. If a
previously saved root has been trashed, Esbern ignores that UUID and links to
an active top-level folder matching the local directory name instead; it does
not restore the trashed folder.

`esbern pull` is one-way: it finds the active top-level reMarkable folder
matching the current directory, downloads new or changed documents, and
does not create, restore, or update anything on the device. Name a remote
folder explicitly with `esbern pull Textbooks`; this creates `./Textbooks`
and pulls the folder's contents into it. Pass `--path ~/reading` to create
`~/reading/Textbooks` instead.

`esbern push` is one-way: it uploads new or changed local books without
performing a device pull. Pass `--path ~/reading` to choose a library root;
otherwise it walks the current directory.

## Download books

`esbern get` wraps the separately installed `libgen-downloader` command.
It searches by title, author, or general terms, downloads into the current
directory, pushes only the successfully downloaded files to reMarkable, and
shows the current phase, filename, elapsed time, and bytes received. Use
`--no-push` for a local-only download. By default it tries EPUB first and falls
back to PDF. When available, LibGen books are matched against Google Books
(embedded ISBN first, validated title/author search second), then saved as
`Author(s) - Title (year).epub` or `.pdf`. If Google Books is unconfigured,
quota-limited, or temporarily unavailable, Esbern instead repairs the LibGen
filename, extracts its author/title/year, reuses embedded EPUB metadata, and
continues the install and targeted push. A daily-quota response temporarily
opens a circuit breaker so bulk work does not repeat doomed requests.

For EPUBs, the resolved title, authors, publication date, publisher,
description, language, subjects, and ISBNs are written into the package
metadata. Google-enriched files also retain the Google volume ID:

```sh
esbern get The Left Hand of Darkness Ursula Le Guin
esbern get "The Left Hand of Darkness" --path ~/Documents/papers
```

Metadata cleanup is on by default. `--no-metadata` is an explicit escape hatch
for a download that should keep its source filename and embedded metadata.
When configured, Google lookup sends the title, author terms, and any embedded
ISBN to Google. The API key is never printed; prefer the environment variable
to a command-line argument so it also stays out of shell history.

Use `--format epub` or `--format pdf` to require one format. For multiple
books, put one query on each non-empty line of a UTF-8 text file:

```sh
esbern get --bulk books.txt
# short form
esbern get -b books.txt
```

LibGen is the default source for title, author, and general book/nonfiction
queries. The same search terms can be sent to arXiv explicitly; use `auto`
when you want Esbern to recognize arXiv IDs/DOIs and fall back between sources:

```sh
esbern get "The WEIRDest People in the World"
esbern get "Attention Is All You Need Ashish Vaswani" --source arxiv
esbern get arXiv:1706.03762 --source auto
```

The single-download display shows the current source/search phase, filename,
bytes received, and elapsed time. Bulk mode presents one live status row per
active worker and reports each result as it completes.

Bulk downloads use four parallel workers by default. Use `--jobs N` (or
`-j N`) to choose between 1 and 32 workers. Queries whose title/author terms
already appear in an EPUB/PDF filename in the destination are skipped, as
are duplicate lines in the input file. Other downloads continue when an
individual query fails, and the command exits non-zero if any book failed.

## Normalize an existing synced library

`esbern normalize` uses the same Google-first, local-fallback resolver for files
already in a synced folder. The default is a read-only preview. With Google
available it refuses ambiguous catalog matches; every mode refuses filename
collisions and by default makes no changes unless every tracked file resolves:

```sh
cd ~/reading/Books
esbern normalize
esbern normalize --apply
```

If a fully resolved run is interrupted, reuse its recovery directory without
performing new catalog requests:

```sh
esbern normalize --resume-plan .esbern/metadata-backups/<timestamp> --apply
```

If the local batch is already complete and only device checkpoints remain,
resume just those entries with `esbern normalize --resume-pending --apply`.
Pending EPUB payloads are uploaded to a temporary device path and atomically
renamed into place, so a slow or interrupted transfer cannot truncate the
existing document payload.

`--apply` preserves each reMarkable UUID and annotation directory. Before the
first device write, it backs up every source, prepares all EPUB metadata in
parallel, and commits every real local payload and canonical filename as one
rollback-safe batch (eight local workers by default). It also moves the
sync-state, tag, and metadata-override keys to those canonical paths. Use
`--workers N` or `ESBERN_METADATA_WORKERS` to choose 1-32 preparation workers.
Only after the complete local library is canonical does it update the EPUB
payload and visible title on each existing device document, checkpointing each
preserved UUID individually. If the device phase is interrupted, pending state
entries remain detectable as local changes so a later normalization or normal
sync can safely finish them.
Original state, device metadata, the plan, and (by default) original local
payloads are kept under `.esbern/metadata-backups/<timestamp>/`. Use
`--allow-unmatched` only to apply the unambiguous subset, and
`--no-backup-files` only when you intentionally do not want payload backups.

For scans, articles, duplicate editions, or a Google catalog correction, add
the tracked relative filename to `.esbern/metadata-overrides.json`. An override
must contain `title`, an `authors` list, and `published_date`; it can also use
any `BookMetadata` field such as `google_id`, `publisher`, `language`,
`categories`, `isbn_10`, or `isbn_13`. Overrides are validated before any
device or local write:

```json
{
  "Unhelpful scan name.pdf": {
    "title": "A Useful Title",
    "authors": ["Ada Lovelace"],
    "published_date": "1843"
  }
}
```

## Remove duplicate downloads

`esbern dedup` recursively hashes PDFs and EPUBs in the current folder and
removes only byte-identical copies. It keeps tracked sync-state files first,
then prefers filenames without `(2)`, `(3)`, and similar copy suffixes.
Duplicates move to a recoverable `.esbern/dedup-trash/<timestamp>` folder:

```sh
esbern dedup --dry-run
esbern dedup
```

Use `--delete` only when you want the duplicate copies permanently deleted.

## Supported file types

`.pdf` and `.epub`. Other files in the synced folder are skipped.

## Caveats

- Pulled PDFs are the raw originals via SFTP, not the annotation-baked
  renders. The annotated render lives behind the device's USB web
  download endpoint, which I haven't wired in yet.
- A manual filesystem-only rename is still treated as delete-on-one-side /
  new-on-the-other. Use `esbern normalize --apply` for catalog-driven renames;
  it preserves the existing device UUID and annotations.
- If you delete a file locally, it stays on the reMarkable. If you
  delete a document on the reMarkable, it stays in the local folder.
  This is intentional. Prune by hand.
- Tested against reMarkable Paper Pro firmware that still ships
  xochitl. If reMarkable swaps in a new document store this will break.
