from __future__ import annotations

import asyncio
from unittest.mock import patch

from esbern.mcp_server import (
    add_book,
    check_book_job,
    get_full_library,
    mcp,
    search_library,
)


def test_mcp_exposes_phone_assistant_tools_with_safe_annotations() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert set(tools) == {
        "get_full_library",
        "search_library",
        "add_book",
        "check_book_job",
    }
    assert tools["get_full_library"].annotations.read_only_hint is True
    assert tools["search_library"].annotations.read_only_hint is True
    assert tools["add_book"].annotations.read_only_hint is False
    assert tools["add_book"].annotations.idempotent_hint is False


@patch("esbern.mcp_server._request")
def test_mcp_read_tools_use_public_catalog_api(request) -> None:
    request.side_effect = [
        {"count": 2, "books": []},
        {"count": 1, "books": []},
    ]

    assert get_full_library()["count"] == 2
    assert search_library("Le Guin & Butler", limit=7)["count"] == 1

    assert request.call_args_list[0].args == ("GET", "/api/books")
    assert request.call_args_list[1].args == (
        "GET",
        "/api/books/search?q=Le+Guin+%26+Butler&limit=7",
    )


@patch("esbern.mcp_server._request")
def test_mcp_write_and_status_tools_authenticate(request) -> None:
    request.side_effect = [
        {"id": "abc", "status": "queued"},
        {"id": "abc", "status": "succeeded"},
    ]

    assert add_book("Kindred Octavia Butler", format="epub")["status"] == "queued"
    assert check_book_job("abc")["status"] == "succeeded"

    assert request.call_args_list[0].args == (
        "POST",
        "/api/jobs/books",
        {
            "query": "Kindred Octavia Butler",
            "format": "epub",
            "source": "libgen",
        },
    )
    assert request.call_args_list[0].kwargs == {"authenticated": True}
    assert request.call_args_list[1].args == ("GET", "/api/jobs/abc")
    assert request.call_args_list[1].kwargs == {"authenticated": True}
