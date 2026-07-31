"""Remote MCP adapter for Claude and other hosted assistants."""

from __future__ import annotations

import json
import os
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

_API_URL = os.environ.get("ESBERN_INTERNAL_API_URL", "http://127.0.0.1:8037").rstrip(
    "/"
)

mcp = MCPServer(
    name="esbern-library",
    title="Esbern Library",
    description="Search a private book library and queue books for installation.",
    instructions=(
        "Before every addition, determine the edition's ISBN-13 without guessing. "
        "Search by ISBN first, then exact title, author, and edition because older "
        "records may not expose ISBN metadata. If either check finds the book, do not "
        "call add_book; return a 'Duplicate book error' and say nothing was added. "
        "Only add when the user clearly asks and no duplicate exists. Report the "
        "background job id and use check_book_job when the user wants its status."
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
def add_book(
    query: str,
    format: Literal["auto", "epub", "pdf"] = "auto",
    source: Literal["auto", "libgen", "arxiv"] = "libgen",
) -> dict[str, Any]:
    """Queue a non-duplicate book for download and automatic reMarkable sync.

    Determine ISBN-13 and search by ISBN, then exact title/author/edition. Never call
    this tool when either check finds the book. Call only after an explicit add request.
    The returned job continues in the background.
    """
    return _request(
        "POST",
        "/api/jobs/books",
        {"query": query, "format": format, "source": source},
        authenticated=True,
    )


@mcp.tool(
    title="Check book job",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def check_book_job(job_id: str) -> dict[str, Any]:
    """Check whether a queued book installation is queued, running, or finished."""
    return _request("GET", f"/api/jobs/{job_id}", authenticated=True)


def main() -> None:
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8038,
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
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
