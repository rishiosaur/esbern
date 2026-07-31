from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from esbern.mcp_server import (
    add_book,
    check_job,
    get_full_library,
    mcp,
    pull_library,
    push_library,
    search_library,
    sync_library,
)


def test_mcp_exposes_phone_assistant_tools_with_safe_annotations() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert "Search by ISBN first" in mcp.instructions
    assert "Duplicate book error" in mcp.instructions
    assert set(tools) == {
        "get_full_library",
        "search_library",
        "add_book",
        "push_library",
        "pull_library",
        "sync_library",
        "check_job",
    }
    assert tools["get_full_library"].annotations.read_only_hint is True
    assert tools["search_library"].annotations.read_only_hint is True
    assert tools["add_book"].annotations.read_only_hint is False
    assert tools["add_book"].annotations.idempotent_hint is False
    assert tools["push_library"].annotations.idempotent_hint is True
    assert tools["pull_library"].annotations.idempotent_hint is True
    assert tools["sync_library"].annotations.read_only_hint is False
    assert tools["sync_library"].annotations.idempotent_hint is True


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
        {
            "id": "abc",
            "status": "queued",
            "progress_events": [{"sequence": 0, "message": "Queued", "detail": None}],
        },
        {
            "id": "abc",
            "status": "running",
            "progress_events": [
                {"sequence": 0, "message": "Queued", "detail": None},
                {
                    "sequence": 1,
                    "message": "Starting LibGen EPUB search",
                    "detail": "Kindred",
                },
            ],
        },
        {
            "id": "abc",
            "status": "succeeded",
            "progress_events": [
                {
                    "sequence": 2,
                    "message": "reMarkable push complete",
                    "detail": "1 file change",
                }
            ],
        },
        {"id": "abc", "status": "succeeded"},
    ]
    context = SimpleNamespace(report_progress=AsyncMock())

    with patch("esbern.mcp_server.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(
            add_book(
                "Kindred Octavia Butler",
                context,
                format="epub",
            )
        )

    assert result["status"] == "succeeded"
    assert check_job("abc")["status"] == "succeeded"
    progress = context.report_progress.await_args_list
    assert [call.args[0] for call in progress] == [1.0, 2.0, 3.0]
    assert progress[0].kwargs["message"] == "Job abc: Queued"
    assert "Starting LibGen EPUB search" in progress[1].kwargs["message"]
    assert "reMarkable push complete" in progress[2].kwargs["message"]

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
    assert request.call_args_list[2].args == ("GET", "/api/jobs/abc")
    assert request.call_args_list[2].kwargs == {"authenticated": True}
    assert request.call_args_list[3].args == ("GET", "/api/jobs/abc")
    assert request.call_args_list[3].kwargs == {"authenticated": True}


@patch("esbern.mcp_server._request")
def test_mcp_push_and_pull_tools_queue_distinct_one_way_jobs(request) -> None:
    request.side_effect = [
        {
            "id": "push-abc",
            "status": "succeeded",
            "progress_events": [
                {
                    "sequence": 0,
                    "message": "Library push finished",
                    "detail": None,
                }
            ],
        },
        {
            "id": "pull-abc",
            "status": "succeeded",
            "progress_events": [
                {
                    "sequence": 0,
                    "message": "Library pull finished",
                    "detail": None,
                }
            ],
        },
    ]
    push_context = SimpleNamespace(report_progress=AsyncMock())
    pull_context = SimpleNamespace(report_progress=AsyncMock())

    pushed = asyncio.run(push_library(push_context, workers=2))
    pulled = asyncio.run(pull_library(pull_context))

    assert pushed["status"] == "succeeded"
    assert pulled["status"] == "succeeded"
    assert request.call_args_list[0].args == (
        "POST",
        "/api/jobs/push",
        {"workers": 2},
    )
    assert request.call_args_list[0].kwargs == {"authenticated": True}
    assert request.call_args_list[1].args == ("POST", "/api/jobs/pull")
    assert request.call_args_list[1].kwargs == {"authenticated": True}


@patch("esbern.mcp_server._request")
def test_mcp_sync_tool_streams_a_queued_sync_job(request) -> None:
    request.side_effect = [
        {
            "id": "sync-abc",
            "status": "queued",
            "progress_events": [{"sequence": 0, "message": "Queued", "detail": None}],
        },
        {
            "id": "sync-abc",
            "status": "running",
            "progress_events": [
                {
                    "sequence": 1,
                    "message": "reMarkable sync: pull",
                    "detail": "checking 12 documents",
                }
            ],
        },
        {
            "id": "sync-abc",
            "status": "succeeded",
            "progress_events": [
                {
                    "sequence": 2,
                    "message": "Library sync finished",
                    "detail": None,
                }
            ],
        },
    ]
    context = SimpleNamespace(report_progress=AsyncMock())

    with patch("esbern.mcp_server.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(sync_library(context, workers=3))

    assert result["status"] == "succeeded"
    messages = [
        call.kwargs["message"] for call in context.report_progress.await_args_list
    ]
    assert messages == [
        "Job sync-abc: Queued",
        "Job sync-abc: reMarkable sync: pull — checking 12 documents",
        "Job sync-abc: Library sync finished",
    ]
    assert request.call_args_list[0].args == (
        "POST",
        "/api/jobs/sync",
        {"workers": 3},
    )
    assert request.call_args_list[0].kwargs == {"authenticated": True}
