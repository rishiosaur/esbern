"""Remote MCP adapter for Claude and other hosted assistants."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

_API_URL = os.environ.get("ESBERN_INTERNAL_API_URL", "http://127.0.0.1:8037").rstrip(
    "/"
)
_JOB_POLL_INTERVAL = 0.5
_TERMINAL_JOB_STATUSES = {"succeeded", "failed"}

mcp = MCPServer(
    name="esbern-library",
    title="Esbern Library",
    description=(
        "Search, add to, normalize, push, pull, and synchronize a private book library."
    ),
    instructions=(
        "Before every addition, determine the edition's ISBN-13 without guessing. "
        "Search by ISBN first, then exact title, author, and edition because older "
        "records may not expose ISBN metadata. If either check finds the book, do not "
        "call add_book; return a 'Duplicate book error' and say nothing was added. "
        "Only add when the user clearly asks and no duplicate exists. Report the "
        "download and targeted reMarkable push progress emitted by add_book. Do not "
        "call push_library after add_book because the add already pushes its new file. "
        "If that tool call "
        "is interrupted, the background job keeps running; use check_job with the "
        "reported job id to recover its latest progress and result. Use push_library "
        "for an explicit one-way local-to-device deployment and pull_library for an "
        "explicit device-to-local retrieval. To normalize an existing book, search "
        "first and pass its exact id to normalize_book. Normalization uses a local "
        "LibGen/EPUB fallback when Google Books is unavailable and does not push an "
        "untracked book. Only call "
        "sync_library when the user explicitly asks to synchronize their library, "
        "then report its live progress and terminal result."
    ),
)


def _request(
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    *,
    authenticated: bool = False,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if authenticated:
        token = os.environ.get("ESBERN_API_TOKEN", "")
        if not token:
            raise RuntimeError("The Esbern connector is missing its API credential.")
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{_API_URL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            detail = json.load(error).get("detail", error.reason)
        except (AttributeError, ValueError):
            detail = error.reason
        raise RuntimeError(f"Esbern API returned {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach the Esbern API: {error.reason}") from error
    if not isinstance(result, dict):
        raise TypeError("Esbern API returned an invalid response.")
    return result


def _progress_message(job_id: str, event: dict[str, object]) -> str:
    message = str(event.get("message") or "Working")
    detail = event.get("detail")
    if detail:
        message = f"{message} — {detail}"
    return f"Job {job_id}: {message}"


async def _relay_progress(
    context: Context,
    record: dict[str, Any],
    *,
    job_id: str,
    after_sequence: int,
) -> int:
    events = record.get("progress_events")
    if not isinstance(events, list):
        current = record.get("progress")
        events = [current] if isinstance(current, dict) else []

    latest = after_sequence
    valid_events = [event for event in events if isinstance(event, dict)]
    valid_events.sort(key=lambda event: int(event.get("sequence", -1)))
    for event in valid_events:
        sequence = int(event.get("sequence", -1))
        if sequence <= latest:
            continue
        await context.report_progress(
            float(sequence + 1),
            message=_progress_message(job_id, event),
        )
        latest = sequence
    return latest


async def _watch_job(
    context: Context,
    record: dict[str, Any],
) -> dict[str, Any]:
    job_id = str(record.get("id") or "")
    if not job_id:
        raise RuntimeError("Esbern queued work without returning a job id.")

    sequence = await _relay_progress(
        context,
        record,
        job_id=job_id,
        after_sequence=-1,
    )
    while record.get("status") not in _TERMINAL_JOB_STATUSES:
        await asyncio.sleep(_JOB_POLL_INTERVAL)
        record = await asyncio.to_thread(
            _request,
            "GET",
            f"/api/jobs/{job_id}",
            authenticated=True,
        )
        sequence = await _relay_progress(
            context,
            record,
            job_id=job_id,
            after_sequence=sequence,
        )
    return record


@mcp.tool(
    title="Get full library",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def get_full_library() -> dict[str, Any]:
    """Return every PDF and EPUB currently in the owner's library."""
    return _request("GET", "/api/books")


@mcp.tool(
    title="Search library",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def search_library(query: str, limit: int = 20) -> dict[str, Any]:
    """Search title, author, year, folder, path, and format in the library."""
    query_string = urlencode({"q": query, "limit": limit})
    return _request("GET", f"/api/books/search?{query_string}")


@mcp.tool(
    title="Add book",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def add_book(
    query: str,
    context: Context,
    format: Literal["auto", "epub", "pdf"] = "auto",
    source: Literal["auto", "libgen", "arxiv"] = "libgen",
) -> dict[str, Any]:
    """Download a non-duplicate book and push it to reMarkable with live progress.

    Determine ISBN-13 and search by ISBN, then exact title/author/edition. Never call
    this tool when either check finds the book. Call only after an explicit add request.
    The underlying job continues in the background if this tool call is interrupted.
    """
    record = await asyncio.to_thread(
        _request,
        "POST",
        "/api/jobs/books",
        {"query": query, "format": format, "source": source},
        authenticated=True,
    )
    return await _watch_job(context, record)


@mcp.tool(
    title="Normalize one existing book",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def normalize_book(
    book_id: str,
    context: Context,
    query: str = "",
    workers: int = 4,
) -> dict[str, Any]:
    """Clean one existing book's filename and metadata with live progress.

    Search first and pass the exact book id. Google Books is tried when available;
    otherwise Esbern cleans LibGen and embedded EPUB metadata locally. A tracked
    book preserves its reMarkable UUID. An untracked book is not pushed by this tool.
    """
    record = await asyncio.to_thread(
        _request,
        "POST",
        "/api/jobs/normalize",
        {
            "book_id": book_id,
            "query": query,
            "workers": workers,
        },
        authenticated=True,
    )
    return await _watch_job(context, record)


@mcp.tool(
    title="Push library to reMarkable",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def push_library(
    context: Context,
    workers: int = 4,
) -> dict[str, Any]:
    """Push local changes to reMarkable without scanning or pulling device books."""
    record = await asyncio.to_thread(
        _request,
        "POST",
        "/api/jobs/push",
        {"workers": workers},
        authenticated=True,
    )
    return await _watch_job(context, record)


@mcp.tool(
    title="Pull library from reMarkable",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def pull_library(context: Context) -> dict[str, Any]:
    """Retrieve device changes without uploading local books, streaming progress."""
    record = await asyncio.to_thread(
        _request,
        "POST",
        "/api/jobs/pull",
        authenticated=True,
    )
    return await _watch_job(context, record)


@mcp.tool(
    title="Synchronize library",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def sync_library(
    context: Context,
    workers: int = 4,
) -> dict[str, Any]:
    """Synchronize every library folder with reMarkable, streaming each phase.

    Call only when the user explicitly asks for a sync. Esbern pulls device changes
    first, safely deduplicates, then pushes local changes without deleting books.
    The underlying job continues in the background if this tool call is interrupted.
    """
    record = await asyncio.to_thread(
        _request,
        "POST",
        "/api/jobs/sync",
        {"workers": workers},
        authenticated=True,
    )
    return await _watch_job(context, record)


@mcp.tool(
    title="Check background job",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def check_job(job_id: str) -> dict[str, Any]:
    """Check any Esbern background job and its persisted progress."""
    return _request("GET", f"/api/jobs/{job_id}", authenticated=True)


def main() -> None:
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8038,
        streamable_http_path="/",
        stateless_http=True,
        json_response=False,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "127.0.0.1:*",
                "localhost:*",
                "[::1]:*",
                "esbern.rishi.cx",
                "esbern.rishi.cx:443",
            ],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
                "https://claude.ai",
                "https://www.claude.ai",
            ],
        ),
    )


if __name__ == "__main__":
    main()
