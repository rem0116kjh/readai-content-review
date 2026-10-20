#!/usr/bin/env python3
"""Local web milestone. Bind to loopback; no external hosting configured."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

import app as desktop
import criteria
from openai_reviewer import OpenAIReviewer
import review

ROOT = Path(__file__).resolve().parent
COOKIE = "readai_session"


def load_env(path):
    """Small dotenv reader; never logs values or runs shell expressions."""
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                os.environ.setdefault(key, value.strip().strip("'\""))


def create_web_app(manager=None, reviewer=None, origin="http://127.0.0.1:8770", password=""):
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("현재 웹 테스트 버전은 http://127.0.0.1 주소만 허용합니다.")
    if password and len(password) < 12:
        raise ValueError("접속 비밀번호는 12자 이상으로 설정하세요.")
    reviewer = reviewer or OpenAIReviewer(os.environ.get("OPENAI_API_KEY", ""), os.environ.get("OPENAI_MODEL", review.DEFAULT_AI_MODEL))
    manager = manager or desktop.ReviewManager(ROOT/".local/web", ai_reviewer=reviewer,
                                               ai_status=reviewer.status, ai_progress=True)
    csrf = secrets.token_urlsafe(32)
    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1) if password else b""
    sessions = {}
    login_failures = []
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(application):
        yield
        manager.cancel()
        if manager.worker:
            await asyncio.to_thread(manager.worker.join, 5)

    api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api.state.manager = manager

    def response_error(message, status):
        return JSONResponse({"error": message}, status_code=status)

    def authenticated(request):
        if not password_hash:
            return True  # loopback-only development; no LAN/public binding
        token = request.cookies.get(COOKIE, "")
        with lock:
            expiry = sessions.get(token, 0)
        return expiry > time.monotonic()

    @api.middleware("http")
    async def boundary(request: Request, call_next):
        if request.headers.get("host") != parsed.netloc or request.headers.get("origin", origin) != origin or request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return response_error("허용되지 않은 접속 주소입니다.", 403)
        if request.method not in ("GET", "POST"):
            return response_error("지원하지 않는 요청입니다.", 405)
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return response_error("JSON 요청이 필요합니다.", 400)
            size = 0
            body = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 16384:
                    return response_error("요청 크기를 초과했습니다.", 413)
                body.append(chunk)
            request._body = b"".join(body)
        public = request.url.path in ("/login", "/login.js", "/styles.css", "/api/login", "/api/health")
        if not public and not authenticated(request):
            if request.url.path == "/":
                return RedirectResponse("/login", status_code=303)
            return response_error("다시 로그인하세요.", 401)
        if request.method == "POST" and request.url.path != "/api/login":
            provided = request.headers.get("x-review-token", "")
            if not provided.isascii() or not secrets.compare_digest(provided, csrf):
                return response_error("화면을 새로고침한 뒤 다시 실행하세요.", 403)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    async def body(request):
        try:
            value = await request.json()
        except (ValueError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @api.get("/api/health")
    def health():
        return {"app_id": "readai-web-review", "engine_version": review.ENGINE_VERSION}

    @api.get("/")
    def home():
        html = (ROOT/"web/index.html").read_text()
        html = html.replace("이 Mac에서 실행 중", "로컬 웹 테스트")
        html = html.replace("AI 검토에는 현재 Codex 사용량이 적용됩니다.", "AI 검토에는 서버의 OpenAI API 사용량이 적용됩니다.")
        html = html.replace('<div class="sidebar-footer">', '<button id="logout" class="secondary" type="button">접속 종료</button><div class="sidebar-footer">') if password_hash else html
        return HTMLResponse(html)

    @api.get("/app.js")
    def javascript():
        return FileResponse(ROOT/"web/app.js", media_type="text/javascript")

    @api.get("/styles.css")
    def stylesheet():
        return FileResponse(ROOT/"web/styles.css", media_type="text/css")

    @api.get("/login")
    def login_page():
        return FileResponse(ROOT/"web/login.html")

    @api.get("/login.js")
    def login_script():
        return FileResponse(ROOT/"web/login.js", media_type="text/javascript")

    @api.post("/api/login")
    async def login(request: Request):
        value = await body(request)
        if value is None or set(value) != {"password"} or not isinstance(value["password"], str) or len(value["password"]) > 256:
            return response_error("접속 비밀번호를 입력하세요.", 400)
        with lock:
            now = time.monotonic()
            login_failures[:] = [t for t in login_failures if t > now - 60]
            if len(login_failures) >= 5:
                return response_error("잠시 후 다시 시도하세요.", 429)
        actual = await asyncio.to_thread(hashlib.scrypt, value["password"].encode(), salt=salt, n=16384, r=8, p=1)
        if not password_hash or not secrets.compare_digest(password_hash, actual):
            with lock:
                login_failures.append(time.monotonic())
            return response_error("접속 비밀번호가 일치하지 않습니다.", 401)
        token = secrets.token_urlsafe(32)
        with lock:
            for stale in [s for s, expiry in sessions.items() if expiry < time.monotonic()]:
                del sessions[stale]
            if len(sessions) >= 32:
                return response_error("접속 수를 초과했습니다. 기존 접속을 종료하세요.", 429)
            sessions[token] = time.monotonic() + 8 * 3600
        result = JSONResponse({"ok": True})
        result.set_cookie(COOKIE, token, max_age=8*3600, httponly=True, samesite="strict", secure=False)
        return result

    @api.post("/api/logout")
    def logout(request: Request):
        with lock:
            sessions.pop(request.cookies.get(COOKIE, ""), None)
        result = JSONResponse({"ok": True})
        result.delete_cookie(COOKIE)
        return result

    @api.get("/api/bootstrap")
    def bootstrap():
        report = manager.poll(compact=True)["job"]
        if report is None:
            report = desktop.public_report(manager.latest())
        return {"token": csrf, "ai": reviewer.status(), "source_url": review.SHEET_URL,
                "criteria": criteria.info(), "engine_version": review.ENGINE_VERSION,
                "saved_sources": [{k: s[k] for k in ("id", "label", "fetched_at")} for s in desktop.saved_sources()],
                "last_report": report, "web_mode": True, "auth_required": bool(password_hash)}

    @api.get("/api/catalog")
    def catalog(saved_source: str = "", sheet_url: str = review.SHEET_URL):
        try:
            snapshot = desktop.read_saved_source(saved_source) if saved_source else {
                "sheets": {"2_세트목록": review.download_tab("2_세트목록", review.parse_sheet_id(sheet_url))}}
            return {"sets": review.catalog(snapshot), "fetched_at": snapshot.get("fetched_at") or review.now(),
                    "source_mode": snapshot.get("source_mode", "live")}
        except Exception:
            return response_error("시트 목록을 읽지 못했습니다. 링크 보기 권한과 시트 형식을 확인하세요.", 502)

    @api.get("/api/status")
    def status(after_id: str = "", after_revision: str = ""):
        revision = int(after_revision) if after_revision.isdecimal() and len(after_revision) < 20 else None
        return manager.poll(after_id, revision, compact=True)

    @api.post("/api/review")
    async def start(request: Request):
        options = await body(request)
        try:
            return JSONResponse({"id": manager.start(options)}, status_code=202)
        except ValueError as exc:
            return response_error(str(exc), 400)
        except RuntimeError as exc:
            return response_error(str(exc), 409)

    @api.post("/api/cancel")
    async def cancel(request: Request):
        data = await body(request)
        if data is None or set(data) != {"id"} or not isinstance(data["id"], str) or not desktop.RUN_ID.fullmatch(data["id"]):
            return response_error("중단할 검토 ID가 필요합니다.", 400)
        try:
            manager.cancel(data["id"])
            return {"ok": True}
        except RuntimeError as exc:
            return response_error(str(exc), 409)

    @api.get("/api/report/{filename}")
    def download(filename: str):
        if not re.fullmatch(r"[0-9a-f]{24}\.(json|md)", filename):
            return response_error("올바른 보고서 주소가 아닙니다.", 404)
        path = manager.output_dir/"reports"/filename
        if not path.is_file():
            return response_error("아직 저장된 보고서가 없습니다.", 404)
        return FileResponse(path, filename="ReadAI-review-"+filename,
                            media_type="application/json" if filename.endswith(".json") else "text/markdown")

    return api


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="리드AI OpenAI API 로컬 웹 테스트")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    load_env(ROOT/".env")
    application = create_web_app(origin=f"http://127.0.0.1:{args.port}",
                                 password=os.environ.get("READAI_ACCESS_PASSWORD", ""))
    uvicorn.run(application, host="127.0.0.1", port=args.port, workers=1,
                access_log=False, log_level="warning")
