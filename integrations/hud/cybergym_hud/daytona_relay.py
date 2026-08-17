"""Task-scoped public relay for private CyberGym submissions from Daytona."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
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
TASK_ID_PATTERN = re.compile(r"^(?:arvo|oss-fuzz):[^:]+$")


def _require_admin(request: Request, admin_token: str) -> None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not secrets.compare_digest(supplied, admin_token):
        raise HTTPException(status_code=401, detail="relay administrator authentication failed")


def _create_binding(registry: Path, task_id: str) -> str:
    if TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise HTTPException(status_code=400, detail="task ID is invalid")
    token = secrets.token_hex(32)
    path = registry / f"{token}.json"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        payload = json.dumps(
            {"task_id": task_id, "expires_at": int(time.time()) + 2 * 60 * 60},
            sort_keys=True,
        ).encode()
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short relay binding write")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return token


def _delete_binding(registry: Path, token: str) -> None:
    _load_binding(registry, token)
    path = registry / f"{token}.json"
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404) from exc


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


def build_app(*, registry: Path, upstream: str, admin_token: str | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    upstream = upstream.rstrip("/")

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    if admin_token is not None:
        if len(admin_token) != 64 or any(character not in "0123456789abcdef" for character in admin_token):
            raise ValueError("relay administrator token must be 32-byte lowercase hexadecimal")

        @app.post("/admin/v1/bindings")
        async def create_binding(request: Request) -> dict[str, str]:
            _require_admin(request, admin_token)
            content_length = request.headers.get("content-length")
            if content_length is None or not content_length.isdigit() or int(content_length) > 4096:
                raise HTTPException(status_code=413, detail="binding request exceeds relay limit")
            try:
                body = await request.json()
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=400, detail="binding request is malformed") from exc
            task_id = body.get("task_id") if isinstance(body, dict) else None
            if not isinstance(task_id, str):
                raise HTTPException(status_code=400, detail="binding request omitted its task ID")
            return {"token": _create_binding(registry, task_id)}

        @app.delete("/admin/v1/bindings/{token}", status_code=204)
        async def delete_binding(token: str, request: Request) -> Response:
            _require_admin(request, admin_token)
            _delete_binding(registry, token)
            return Response(status_code=204)

        @app.post("/admin/v1/grader/{operation}")
        async def coordinator_grader(operation: str, request: Request) -> Response:
            _require_admin(request, admin_token)
            if operation not in {"verify-agent-pocs", "query-poc"}:
                raise HTTPException(status_code=404)
            content_length = request.headers.get("content-length")
            if content_length is None or not content_length.isdigit() or int(content_length) > 4096:
                raise HTTPException(status_code=413, detail="grader request exceeds relay limit")
            try:
                body = await request.json()
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=400, detail="grader request is malformed") from exc
            agent_id = body.get("agent_id") if isinstance(body, dict) else None
            if (
                not isinstance(agent_id, str)
                or len(agent_id) != 32
                or any(character not in "0123456789abcdef" for character in agent_id)
            ):
                raise HTTPException(status_code=400, detail="grader request omitted its agent identity")
            upstream_key = os.environ.get("CYBERGYM_API_KEY", "").strip()
            if not upstream_key:
                raise HTTPException(status_code=503, detail="relay grader credential is unavailable")
            async with httpx.AsyncClient(timeout=1200) as client:
                response = await client.post(
                    f"{upstream}/{operation}",
                    headers={"X-API-Key": upstream_key},
                    json={"agent_id": agent_id},
                )
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type"),
            )

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
    parser.add_argument("--enable-admin", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    args.registry.mkdir(parents=True, exist_ok=True)
    os.chmod(args.registry, 0o700)
    admin_token = os.environ.get("CG_DAYTONA_RELAY_ADMIN_TOKEN", "").strip() if args.enable_admin else None
    if args.enable_admin and not admin_token:
        raise SystemExit("CG_DAYTONA_RELAY_ADMIN_TOKEN is required with --enable-admin")
    uvicorn.run(
        build_app(registry=args.registry.resolve(), upstream=args.upstream, admin_token=admin_token),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
