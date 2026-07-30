"""Minimal FastAPI server for browsing, downloading, and syncing books."""

from __future__ import annotations

import html
import os
import secrets
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from esbern import __version__
from esbern.server_library import (
    ServerInputError,
    catalog,
    cover,
    install_books,
    synchronize,
)

_MAX_BULK_BYTES = 1024 * 1024


class InstallBookRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    format: Literal["auto", "epub", "pdf"] = "auto"
    source: Literal["auto", "libgen", "arxiv"] = "libgen"
    metadata: bool = True
    jobs: int = Field(default=1, ge=1, le=32)
    sync_workers: int = Field(default=4, ge=1, le=8)


class SyncRequest(BaseModel):
    workers: int = Field(default=4, ge=1, le=8)


def _mutation_status(result: dict[str, object], *, bulk: bool) -> int:
    sync = result.get("sync")
    if isinstance(sync, dict) and sync.get("ok") is False:
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
<body><main aria-label="{escaped_library} library">{''.join(images)}</main></body>
</html>"""


def create_app(library_root: Path) -> FastAPI:
    root = library_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Library path is not a directory: {root}")

    app = FastAPI(
        title="Esbern",
        description="Download books into a server-side library and sync it with reMarkable.",
        version=__version__,
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
            raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")

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
                "install": "POST /api/books",
                "bulk_install": "POST text/plain to /api/books/bulk",
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

    @app.post(
        "/api/books",
        tags=["books"],
        dependencies=[Depends(authorize)],
    )
    def install_book(request: InstallBookRequest, response: Response) -> dict[str, object]:
        payload = request.model_dump()
        payload["queries"] = [payload.pop("query")]
        result = install_books(root, payload)
        response.status_code = _mutation_status(result, bulk=False)
        return result

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
            raise HTTPException(status_code=400, detail="Bulk text must be UTF-8.") from error
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
                "sync_workers": _integer_query(request, "sync_workers", 4),
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


def _integer_query(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"{name} must be an integer.") from error


def serve_web(library_root: Path, *, hostname: str, port: int) -> None:
    root = library_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Library path is not a directory: {root}")
    uvicorn.run(create_app(root), host=hostname, port=port)
