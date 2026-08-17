"""Task-scoped public relay for private CyberGym submissions from Daytona."""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

MAX_POC_BYTES = 64 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_POC_BYTES + 1024 * 1024


def _load_binding(registry: Path, token: str) -> dict[str, object]:
    if not token or len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise HTTPException(status_code=404)
    path = registry / f"{token}.json"
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise HTTPException(status_code=404)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404) from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("task_id"), str)
        or not isinstance(value.get("expires_at"), int | float)
        or float(value["expires_at"]) < time.time()
    ):
        raise HTTPException(status_code=404)
    return value


def build_app(*, registry: Path, upstream: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    upstream = upstream.rstrip("/")

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.api_route("/{token}/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    async def relay(token: str, rest: str, request: Request) -> Response:
        binding = _load_binding(registry, token)
        if request.method != "POST" or rest != "submit-vul":
            raise HTTPException(status_code=404)
        content_length = request.headers.get("content-length")
        if content_length is None or not content_length.isdigit() or int(content_length) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="submission exceeds relay limit")
        form = await request.form()
        metadata = form.get("metadata")
        upload = form.get("file")
        if not isinstance(metadata, str) or not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail="submission form is malformed")
        try:
            parsed = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="submission metadata is malformed") from exc
        if not isinstance(parsed, dict) or parsed.get("task_id") != binding["task_id"]:
            raise HTTPException(status_code=403, detail="submission task binding mismatch")
        content = await upload.read(MAX_POC_BYTES + 1)
        if len(content) > MAX_POC_BYTES:
            raise HTTPException(status_code=413, detail="PoC exceeds relay limit")
        async with httpx.AsyncClient(timeout=1200) as client:
            response = await client.post(
                f"{upstream}/submit-vul",
                data={"metadata": metadata},
                files={
                    "file": (
                        upload.filename or "poc",
                        content,
                        upload.content_type or "application/octet-stream",
                    )
                },
            )
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    return parser


def main() -> None:
    args = _parser().parse_args()
    args.registry.mkdir(parents=True, exist_ok=True)
    os.chmod(args.registry, 0o700)
    uvicorn.run(
        build_app(registry=args.registry.resolve(), upstream=args.upstream),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
