"""Minimal FastAPI server for browsing, downloading, and syncing books."""

from __future__ import annotations

import html
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from esbern import __version__
from esbern.server_jobs import JobStore
from esbern.server_library import (
    ServerInputError,
    catalog,
    cover,
    install_books,
    search_catalog,
    synchronize,
)

_MAX_BULK_BYTES = 1024 * 1024


class InstallBookRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    format: Literal["auto", "epub", "pdf"] = "auto"
    source: Literal["auto", "libgen", "arxiv"] = "libgen"
    metadata: bool = True
    jobs: int = Field(default=1, ge=1, le=32)
    push_workers: int = Field(default=4, ge=1, le=8)


class SyncRequest(BaseModel):
    workers: int = Field(default=4, ge=1, le=8)


def _mutation_status(result: dict[str, object], *, bulk: bool) -> int:
    push = result.get("push")
    if isinstance(push, dict) and push.get("ok") is False:
        return 502
    failed = result.get("failed")
    if isinstance(failed, list) and failed:
        return 207 if bulk and result.get("downloaded") else 422
    return 201 if result.get("downloaded") else 200


def _grid_html(library: str, books: list[dict[str, object]]) -> str:
    images = []
    for book in books:
        cover_url = html.escape(str(book["cover_url"]), quote=True)
        title = html.escape(str(book["title"]), quote=True)
        images.append(
            f'<img src="{cover_url}" '
            f'alt="{title}" width="800" height="1200" loading="lazy">'
        )
    escaped_library = html.escape(library, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_library}</title>
  <style>
    :root {{ color-scheme: light; background: #e8e5df; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; min-height: 100%; }}
    main {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(8rem, 1fr));
      gap: 1px;
      padding: 1px;
      background: #d1cec7;
    }}
    img {{
      display: block;
      width: 100%;
      height: auto;
      aspect-ratio: 2 / 3;
      object-fit: cover;
      background: #d9d6cf;
    }}
    @media (min-width: 48rem) {{
      main {{ grid-template-columns: repeat(auto-fill, minmax(10rem, 1fr)); }}
    }}
  </style>
</head>
<body><main aria-label="{escaped_library} library">{"".join(images)}</main></body>
</html>"""


def create_app(library_root: Path) -> FastAPI:
    root = library_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Library path is not a directory: {root}")

    jobs = JobStore(root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        jobs.close()

    app = FastAPI(
        title="Esbern",
        description="Download books into a server-side library and sync it with reMarkable.",
        version=__version__,
        lifespan=lifespan,
    )
    cors_origin = os.environ.get("ESBERN_CORS_ORIGIN", "*").strip() or "*"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[cors_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def authorize(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        token = os.environ.get("ESBERN_API_TOKEN", "")
        if not token:
            return
        supplied = ""
        if authorization and authorization.startswith("Bearer "):
            supplied = authorization.removeprefix("Bearer ")
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(
                status_code=401, detail="Invalid or missing bearer token."
            )

    @app.exception_handler(ServerInputError)
    async def server_input_error(_: Request, error: ServerInputError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def library_grid() -> HTMLResponse:
        result = catalog(root)
        books = result["books"]
        assert isinstance(books, list)
        return HTMLResponse(
            _grid_html(str(result["library"]), books),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api", tags=["service"])
    def api_index() -> dict[str, object]:
        return {
            "name": "esbern",
            "docs": "/docs",
            "endpoints": {
                "catalog": "GET /api/books",
                "search": "GET /api/books/search?q=...",
                "install": "POST /api/books",
                "bulk_install": "POST text/plain to /api/books/bulk",
                "queue_install": "POST /api/jobs/books",
                "queue_push": "POST /api/jobs/push",
                "queue_pull": "POST /api/jobs/pull",
                "queue_sync": "POST /api/jobs/sync",
                "job_status": "GET /api/jobs/{job_id}",
                "sync": "POST /api/sync",
            },
        }

    @app.get("/api/health", tags=["service"])
    def health() -> dict[str, object]:
        return {"ok": True, "library": root.name}

    @app.get("/api/books", tags=["books"])
    def list_books(response: Response) -> dict[str, object]:
        response.headers["Cache-Control"] = "no-store"
        return catalog(root)

    @app.get("/api/books/search", tags=["books"])
    def search_books(
        response: Response,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "no-store"
        return search_catalog(root, q, limit=limit)

    @app.get(
        "/integrations/chatgpt/openapi.json",
        tags=["integrations"],
        include_in_schema=False,
    )
    def chatgpt_schema() -> dict[str, object]:
        return _chatgpt_action_schema()

    @app.post(
        "/api/books",
        tags=["books"],
        dependencies=[Depends(authorize)],
    )
    def install_book(
        request: InstallBookRequest, response: Response
    ) -> dict[str, object]:
        payload = request.model_dump()
        payload["queries"] = [payload.pop("query")]
        result = install_books(root, payload)
        response.status_code = _mutation_status(result, bulk=False)
        return result

    @app.post(
        "/api/jobs/books",
        status_code=202,
        tags=["jobs"],
        dependencies=[Depends(authorize)],
    )
    def queue_book(
        request: InstallBookRequest,
        response: Response,
    ) -> dict[str, object]:
        payload = request.model_dump()
        payload["queries"] = [payload.pop("query")]
        record = jobs.enqueue_book(payload)
        response.headers["Location"] = f"/api/jobs/{record['id']}"
        return record

    @app.post(
        "/api/jobs/sync",
        status_code=202,
        tags=["jobs"],
        dependencies=[Depends(authorize)],
    )
    def queue_sync(
        request: SyncRequest,
        response: Response,
    ) -> dict[str, object]:
        record = jobs.enqueue_sync(request.model_dump())
        response.headers["Location"] = f"/api/jobs/{record['id']}"
        return record

    @app.post(
        "/api/jobs/push",
        status_code=202,
        tags=["jobs"],
        dependencies=[Depends(authorize)],
    )
    def queue_push(
        request: SyncRequest,
        response: Response,
    ) -> dict[str, object]:
        record = jobs.enqueue_push(request.model_dump())
        response.headers["Location"] = f"/api/jobs/{record['id']}"
        return record

    @app.post(
        "/api/jobs/pull",
        status_code=202,
        tags=["jobs"],
        dependencies=[Depends(authorize)],
    )
    def queue_pull(response: Response) -> dict[str, object]:
        record = jobs.enqueue_pull({})
        response.headers["Location"] = f"/api/jobs/{record['id']}"
        return record

    @app.get(
        "/api/jobs/{job_id}",
        tags=["jobs"],
        dependencies=[Depends(authorize)],
    )
    def get_job(job_id: str) -> dict[str, object]:
        try:
            return jobs.get(job_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="Job not found.") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/books/bulk",
        tags=["books"],
        dependencies=[Depends(authorize)],
    )
    async def install_bulk(request: Request, response: Response) -> dict[str, object]:
        body = await request.body()
        if len(body) > _MAX_BULK_BYTES:
            raise HTTPException(status_code=413, detail="Bulk request exceeds 1 MiB.")
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type not in {"text/plain", "application/octet-stream"}:
            raise HTTPException(
                status_code=415,
                detail="Bulk requests must contain raw UTF-8 text with one query per line.",
            )
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise HTTPException(
                status_code=400, detail="Bulk text must be UTF-8."
            ) from error
        queries = [line.strip() for line in text.splitlines() if line.strip()]
        result = await run_in_threadpool(
            install_books,
            root,
            {
                "queries": queries,
                "format": request.query_params.get("format", "auto"),
                "source": request.query_params.get("source", "libgen"),
                "metadata": request.query_params.get("metadata", "true").casefold()
                not in {"0", "false", "no"},
                "jobs": _integer_query(request, "jobs", 4),
                "push_workers": _integer_query(request, "push_workers", 4),
            },
        )
        response.status_code = _mutation_status(result, bulk=True)
        return result

    @app.get("/api/books/{book_id}/cover", tags=["books"])
    def book_cover(book_id: str, request: Request) -> Response:
        try:
            image = cover(root, book_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        etag = f'"{book_id}-{image.version}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        cache_control = (
            "public, max-age=31536000, immutable"
            if request.query_params.get("v") == image.version
            else "public, max-age=0, must-revalidate"
        )
        return Response(
            image.data,
            media_type=image.content_type,
            headers={
                "Cache-Control": cache_control,
                "ETag": etag,
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(
        "/api/sync",
        tags=["sync"],
        dependencies=[Depends(authorize)],
    )
    def sync_library(request: SyncRequest) -> dict[str, object]:
        try:
            return synchronize(root, workers=request.workers)
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    return app


def _chatgpt_action_schema() -> dict[str, object]:
    book_response = {"type": "object", "additionalProperties": True}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Esbern Library",
            "description": (
                "Search the owner's book library and queue new books for download "
                "and targeted reMarkable push. Before adding, resolve ISBN-13, "
                "search by ISBN, then confirm by exact title, author, and edition. "
                "Do not queue a duplicate."
            ),
            "version": __version__,
        },
        "servers": [{"url": "https://esbern.rishi.cx"}],
        "paths": {
            "/api/books": {
                "get": {
                    "operationId": "listLibrary",
                    "summary": "Get the full library",
                    "description": "Return every PDF and EPUB in the library.",
                    "responses": {
                        "200": {
                            "description": "Full catalog",
                            "content": {"application/json": {"schema": book_response}},
                        }
                    },
                }
            },
            "/api/books/search": {
                "get": {
                    "operationId": "searchLibrary",
                    "summary": "Search the library",
                    "description": (
                        "Search title, author, year, folder, path, and format. Before "
                        "queueing, search ISBN-13 first and then exact title, author, "
                        "and edition because older records may not expose ISBN."
                    ),
                    "parameters": [
                        {
                            "name": "q",
                            "in": "query",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 500,
                            },
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 500,
                                "default": 20,
                            },
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Ranked matching books",
                            "content": {"application/json": {"schema": book_response}},
                        }
                    },
                }
            },
            "/api/jobs/books": {
                "post": {
                    "operationId": "queueBook",
                    "summary": "Queue a book for installation",
                    "description": (
                        "Queue one non-duplicate book for download into Books and "
                        "an automatic targeted reMarkable push. Call only after ISBN "
                        "and exact title-author-edition searches find no match. Return "
                        "immediately with a job id."
                    ),
                    "security": [{"BearerAuth": []}],
                    "x-openai-isConsequential": True,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["query"],
                                    "properties": {
                                        "query": {
                                            "type": "string",
                                            "minLength": 3,
                                            "maxLength": 500,
                                        },
                                        "format": {
                                            "type": "string",
                                            "enum": ["auto", "epub", "pdf"],
                                            "default": "auto",
                                        },
                                        "source": {
                                            "type": "string",
                                            "enum": ["auto", "libgen", "arxiv"],
                                            "default": "libgen",
                                        },
                                        "metadata": {
                                            "type": "boolean",
                                            "default": True,
                                        },
                                        "jobs": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 32,
                                            "default": 1,
                                        },
                                        "push_workers": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 8,
                                            "default": 4,
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "202": {
                            "description": "Queued job",
                            "content": {"application/json": {"schema": book_response}},
                        }
                    },
                }
            },
            "/api/jobs/{job_id}": {
                "get": {
                    "operationId": "getJob",
                    "summary": "Check a background job",
                    "description": (
                        "Return queued, running, succeeded, or failed state, "
                        "progress events, and the final result for any library job."
                    ),
                    "security": [{"BearerAuth": []}],
                    "parameters": [
                        {
                            "name": "job_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Current job state",
                            "content": {"application/json": {"schema": book_response}},
                        }
                    },
                }
            },
            "/api/jobs/sync": {
                "post": {
                    "operationId": "queueLibrarySync",
                    "summary": "Queue a full library sync",
                    "description": (
                        "Queue a two-way reconciliation of every configured library "
                        "folder with the reMarkable. Return immediately with a job id."
                    ),
                    "security": [{"BearerAuth": []}],
                    "x-openai-isConsequential": True,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "workers": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 8,
                                            "default": 4,
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "202": {
                            "description": "Queued sync job",
                            "content": {"application/json": {"schema": book_response}},
                        }
                    },
                }
            },
            "/api/jobs/push": {
                "post": {
                    "operationId": "queueLibraryPush",
                    "summary": "Queue a one-way library push",
                    "description": (
                        "Push local PDF and EPUB changes to reMarkable without "
                        "scanning or downloading device books."
                    ),
                    "security": [{"BearerAuth": []}],
                    "x-openai-isConsequential": True,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "workers": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 8,
                                            "default": 4,
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "202": {
                            "description": "Queued push job",
                            "content": {"application/json": {"schema": book_response}},
                        }
                    },
                }
            },
            "/api/jobs/pull": {
                "post": {
                    "operationId": "queueLibraryPull",
                    "summary": "Queue a one-way library pull",
                    "description": (
                        "Retrieve new and changed books from reMarkable without "
                        "uploading local books."
                    ),
                    "security": [{"BearerAuth": []}],
                    "x-openai-isConsequential": True,
                    "responses": {
                        "202": {
                            "description": "Queued pull job",
                            "content": {"application/json": {"schema": book_response}},
                        }
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}}
        },
    }


def _integer_query(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail=f"{name} must be an integer."
        ) from error


def serve_web(library_root: Path, *, hostname: str, port: int) -> None:
    root = library_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Library path is not a directory: {root}")
    uvicorn.run(create_app(root), host=hostname, port=port)
