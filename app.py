#!/usr/bin/env python3
"""Local GUI server. Run with Python 3.11+; no pip packages required."""
from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import review
import criteria

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / ".local"
APP_ID = "readai-sheet-review-v1"
RUN_ID = re.compile(r"[0-9a-f]{24}")


def saved_sources():
    path = LOCAL / "test-sources.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def read_saved_source(key):
    matches = [s for s in saved_sources() if s["id"] == key]
    if len(matches) != 1:
        raise ValueError("저장한 검토 데이터를 찾을 수 없습니다.")
    path = (LOCAL / matches[0]["path"]).resolve()
    if not path.is_relative_to(LOCAL.resolve()):
        raise ValueError("허용되지 않은 데이터 경로입니다.")
    snapshot = json.loads(path.read_text())
    snapshot["source_mode"] = "snapshot"
    return snapshot


def atomic_write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def public_report(report):
    """UI payload; full model histories remain in immutable report downloads."""
    if report is None:
        return None
    result = {k: v for k, v in report.items() if k != "sets"}
    result["sets"] = []
    for item in report.get("sets", []):
        visible = {k: v for k, v in item.items() if k not in ("ai_passes", "previous_failed_attempt")}
        if "ai_passes" in item:
            visible["ai_passes"] = {
                direction: {k: v for k, v in values.items() if k != "result"}
                for direction, values in item["ai_passes"].items()
            }
        result["sets"].append(visible)
    return copy.deepcopy(result)


def validate_options(options):
    if not isinstance(options, dict) or set(options) - {"grades", "ai", "include_sample", "sheet_url", "saved_source", "set_id"}:
        raise ValueError("알 수 없는 검토 설정입니다.")
    grades = options.get("grades", [1, 2, 3, 4, 5, 6])
    if not isinstance(grades, list) or not grades or len(grades) > 6 or any(type(g) is not int or g not in range(1, 7) for g in grades):
        raise ValueError("1~6학년 중 하나 이상을 선택하세요.")
    for name, default in (("ai", True), ("include_sample", False)):
        if type(options.get(name, default)) is not bool:
            raise ValueError("검토 설정이 올바르지 않습니다.")
    sid, saved = options.get("set_id"), options.get("saved_source")
    for value in (sid, saved):
        if value is not None and (not isinstance(value, str) or len(value) > 100):
            raise ValueError("세트 또는 저장 데이터 선택 값이 올바르지 않습니다.")
    address = options.get("sheet_url", review.SHEET_URL)
    if not isinstance(address, str) or len(address) > 2048:
        raise ValueError("올바른 Google Sheets 주소를 입력하세요.")
    if not saved:
        review.parse_sheet_id(address)
    elif not any(s.get("id") == saved for s in saved_sources()):
        raise ValueError("저장한 검토 데이터를 찾을 수 없습니다.")
    return {"grades": sorted(set(grades)), "ai": options.get("ai", True),
            "include_sample": options.get("include_sample", False), "set_id": sid or None,
            "saved_source": saved or None, "sheet_url": address}


class ReviewManager:
    def __init__(self, output_dir=LOCAL, downloader=None, ai_reviewer=None, ai_status=None, ai_progress=False):
        self.output_dir = Path(output_dir)
        self.downloader = downloader or review.download_snapshot
        self.ai_reviewer = ai_reviewer or review.ai_review
        self.ai_status = ai_status
        self.ai_progress = ai_progress
        self.lock = threading.RLock()
        self.job = None
        self.worker = None
        self.cancel_event = threading.Event()

    def current(self):
        with self.lock:
            return copy.deepcopy(self.job)

    def update(self, **values):
        with self.lock:
            self.job.update(values)
            self._changed()

    def _changed(self):
        """Call while holding lock whenever any visible job content changes."""
        self.job["revision"] = self.job.get("revision", 0) + 1

    def poll(self, after_id=None, after_revision=None, compact=False):
        with self.lock:
            if self.job and self.job["id"] == after_id and self.job.get("revision") == after_revision:
                return {"job": None, "unchanged": True}
            return {"job": public_report(self.job) if compact else copy.deepcopy(self.job)}

    def start(self, options):
        options = validate_options(options)
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise RuntimeError("이미 검토가 실행 중입니다.")
            self.cancel_event = threading.Event()
            self.job = {"id": secrets.token_hex(12), "started_at": review.now(), "status": "running",
                        "message": "최신 시트를 읽습니다", "options": options, "sets": [], "issues": [],
                        "skipped": [], "completed_sets": 0, "total_sets": 0, "error": None,
                        "source_url": review.SHEET_URL, "scope": ["2", "3", "4", "6"],
                        "criteria_version": criteria.VERSION, "criteria_sha256": criteria.HASH, "revision": 1}
            self.job["engine_version"] = review.ENGINE_VERSION
            job_id = self.job["id"]
            self.worker = threading.Thread(target=self._run, args=(options,), daemon=True)
            self.worker.start()
            return job_id

    def cancel(self, expected_id=None):
        with self.lock:
            if expected_id is not None and (not self.job or self.job["id"] != expected_id):
                raise RuntimeError("화면의 검토와 현재 실행 중인 검토가 다릅니다. 최신 상태를 확인하세요.")
            if self.job and self.job["status"] == "running":
                self.job.update(status="cancelling", message="현재 작업을 중단하고 부분 결과를 저장합니다")
                self._changed()
                self.cancel_event.set()

    def persist(self):
        report = self.current()
        path = self.output_dir / "reports" / report["id"]
        atomic_write(path.with_suffix(".json"), json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        atomic_write(path.with_suffix(".md"), review.markdown_report(report))

    def _run(self, options):
        try:
            if options.get("saved_source"):
                snapshot = read_saved_source(options["saved_source"])
            else:
                snapshot = self.downloader(lambda msg: self.update(message=msg), self.cancel_event,
                                           review.parse_sheet_id(options.get("sheet_url", review.SHEET_ID)))
            if self.cancel_event.is_set():
                raise review.Cancelled()
            snapshot = criteria.prepare(snapshot)
            self.update(source_url=review.source_url(snapshot), source_mode=snapshot.get("source_mode", "live"))
            items = review.catalog(snapshot)
            selected, skipped = [], []
            for item in items:
                if item["grade"] not in options.get("grades", [1, 2, 3, 4, 5, 6]):
                    reason = "선택 학년 밖"
                elif options.get("set_id") and options["set_id"] != item["id"]:
                    reason = "다른 세트 선택"
                elif item["sample"] and not options.get("include_sample", False):
                    reason = "견본 제외 설정"
                else:
                    selected.append({**item, "rule_status": "pending", "ai_status": "pending" if options.get("ai", True) else "skipped"})
                    continue
                skipped.append({"id": item["id"], "reason": reason})
            empty_grades = [g for g in options.get("grades", [1, 2, 3, 4, 5, 6]) if not any(i["grade"] == g for i in items)]
            self.update(empty_grades=empty_grades, skipped=skipped, fetched_at=snapshot["fetched_at"])
            if not selected:
                if options.get("set_id"):
                    raise ValueError("선택한 세트를 찾지 못했습니다. 목록을 새로 불러오세요.")
                self.update(status="complete", message="해당 조건의 세트 데이터가 없어 건너뛰었습니다", finished_at=review.now())
                return
            self.update(sets=selected, skipped=skipped, total_sets=len(selected), fetched_at=snapshot["fetched_at"],
                        source_url=review.source_url(snapshot), source_mode=snapshot.get("source_mode", "live"),
                        source_hashes={t: s.get("sha256") or review.table_hash(s) for t, s in snapshot["sheets"].items()})
            # Preserve precisely what was read. Never substitute an old snapshot on a network failure.
            atomic_write(self.output_dir / "snapshots" / f"{self.job['id']}.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
            # Run all structural checks first, so useful results remain if AI is cancelled or fails.
            for index, item in enumerate(selected):
                if self.cancel_event.is_set():
                    raise review.Cancelled()
                self.update(message=f"형식 검사 · {item['id']} ({index + 1}/{len(selected)})")
                findings = review.rule_review(snapshot, item)
                with self.lock:
                    self.job["issues"].extend(findings)
                    self.job["sets"][index]["rule_status"] = "complete"
                    self.job["sets"][index]["review_summary"] = criteria.summary(snapshot, self.job["sets"][index], self.job["issues"])
                    self._changed()
            if self.cancel_event.is_set():
                raise review.Cancelled()
            if not options.get("ai", True):
                # All rule results are ready. Save once in finally, not once per skipped AI step.
                self.update(completed_sets=len(selected), status="complete", message="검토가 완료되었습니다", finished_at=review.now())
                return
            self.persist()
            if self.ai_reviewer is review.ai_review or self.ai_status is not None:
                connection = (self.ai_status or review.codex_status)()
                if not connection["ready"]:
                    with self.lock:
                        for item in self.job["sets"]:
                            item.update(ai_status="failed", ai_error=connection["message"])
                            item["review_summary"] = criteria.summary(snapshot, item, self.job["issues"])
                        self._changed()
                    self.update(status="partial", completed_sets=len(selected), error=connection["message"],
                                message="형식 검사 완료 · AI 연결을 준비한 뒤 다시 검토하세요", finished_at=review.now())
                    return
            for index, item in enumerate(selected):
                if self.cancel_event.is_set():
                    raise review.Cancelled()
                if options.get("ai", True):
                    self.update(message=f"AI 검토 · {item['id']} ({index + 1}/{len(selected)})")
                    with self.lock:
                        self.job["sets"][index]["ai_status"] = "running"
                        self._changed()
                    try:
                        def pass_progress(direction, values):
                            with self.lock:
                                record = self.job["sets"][index].setdefault("ai_passes", {}).setdefault(direction, {})
                                record.update(values)
                                label = "정순 2→3→6→5" if direction == "forward" else "역순 5→6→3→2"
                                self.job["message"] = f"AI {label} · {item['id']} ({index + 1}/{len(selected)})"
                                self._changed()
                            self.persist()
                        if self.ai_reviewer is review.ai_review or self.ai_progress:
                            result = self.ai_reviewer(snapshot, item, self.cancel_event, progress=pass_progress)
                        else:
                            result = self.ai_reviewer(snapshot, item, self.cancel_event)
                        with self.lock:
                            findings = review.assess_ai_findings(snapshot, result["issues"])
                            self.job["issues"] = review.consolidate_findings(self.job["issues"] + findings)
                            self.job["sets"][index].update(ai_status="complete", ai_summary=result["summary"],
                                                            ai_difficulty=result.get("difficulty"), ai_section_results=result.get("section_results", {}))
                            self._changed()
                    except review.Cancelled:
                        raise
                    except review.AIUnavailable as exc:
                        with self.lock:
                            for pending_index in range(index, len(selected)):
                                pending = self.job["sets"][pending_index]
                                pending.update(ai_status="failed", ai_error=str(exc) if pending_index == index
                                               else "실행 안 함 · 앞선 AI 요청이 차단되었습니다. " + str(exc))
                                pending["review_summary"] = criteria.summary(snapshot, pending, self.job["issues"])
                            self._changed()
                        self.update(status="partial", completed_sets=len(selected), error=str(exc),
                                    message="형식 검사 완료 · AI 요청이 차단되어 나머지 검토를 중단했습니다",
                                    finished_at=review.now())
                        return
                    except Exception as exc:
                        with self.lock:
                            self.job["sets"][index].update(ai_status="failed", ai_error=str(exc) if isinstance(exc, ValueError) else "AI 응답을 처리하지 못했습니다.")
                            self._changed()
                with self.lock:
                    self.job["sets"][index]["review_summary"] = criteria.summary(snapshot, self.job["sets"][index], self.job["issues"])
                    self._changed()
                self.update(completed_sets=index + 1)
                self.persist()
            failures = sum(s["ai_status"] == "failed" for s in self.current()["sets"])
            self.update(status="partial" if failures else "complete",
                        message=f"검토 종료 · AI 미완료 {failures}세트" if failures else "검토가 완료되었습니다", finished_at=review.now())
        except review.Cancelled:
            with self.lock:
                for item in self.job["sets"]:
                    if item["ai_status"] == "running":
                        item["ai_status"] = "cancelled"
                self._changed()
            self.update(status="cancelled", message="검토를 중단했습니다. 확인한 결과는 보존했습니다.", finished_at=review.now())
        except Exception as exc:
            error = str(exc) if isinstance(exc, ValueError) else "시트를 읽거나 검토하지 못했습니다. 네트워크와 링크 보기 권한을 확인하세요."
            self.update(status="failed", message="검토를 완료하지 못했습니다", error=error, finished_at=review.now())
        finally:
            try:
                self.persist()
            except OSError:
                self.update(status="failed", error="보고서 저장에 실패했습니다. 폴더 쓰기 권한과 디스크 공간을 확인하세요.")

    def latest(self):
        folder = self.output_dir / "reports"
        for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                report = json.loads(path.read_text())
                if not isinstance(report, dict) or not isinstance(report.get("sets", []), list):
                    continue
                # A server termination must never leave a restored job appearing to still run.
                if report.get("status") in ("running", "cancelling"):
                    report.update(status="interrupted", message="이전 실행이 종료되기 전에 프로그램이 닫혔습니다. 부분 결과입니다.")
                    for item in report.get("sets", []):
                        if item.get("ai_status") == "running":
                            item["ai_status"] = "cancelled"
                        for stage in item.get("ai_passes", {}).values():
                            if stage.get("status") == "running":
                                stage["status"] = "cancelled"
                return report
            except (ValueError, OSError):
                continue
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ReadAI/1.0"

    def log_message(self, *_):
        pass

    def send_bytes(self, body, content_type="application/json; charset=utf-8", status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode(), status=status)

    def allowed_host(self):
        return self.headers.get_all("Host") == [f"127.0.0.1:{self.server.server_port}"]

    def same_origin(self):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        return (self.headers.get("Origin", origin) == origin
                and self.headers.get("Sec-Fetch-Site") not in ("cross-site", "same-site"))

    def do_GET(self):
        if not self.allowed_host() or not self.same_origin():
            return self.json({"error": "허용되지 않은 접속 주소입니다."}, 403)
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            return self.json({"app_id": APP_ID, "engine_version": review.ENGINE_VERSION})
        if path == "/api/bootstrap":
            last = self.server.manager.poll(compact=True)["job"]
            if last is None:
                last = public_report(self.server.manager.latest())
            return self.json({"token": self.server.token, "ai": review.codex_status(), "source_url": review.SHEET_URL,
                              "criteria": criteria.info(), "engine_version": review.ENGINE_VERSION,
                              "saved_sources": [{k: s[k] for k in ("id", "label", "fetched_at")} for s in saved_sources()],
                              "last_report": last})
        if path == "/api/catalog":
            try:
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if params.get("saved_source"):
                    snapshot = read_saved_source(params["saved_source"][0])
                else:
                    sid = review.parse_sheet_id(params.get("sheet_url", [review.SHEET_ID])[0])
                    snapshot = {"sheets": {"2_세트목록": review.download_tab("2_세트목록", sid)}}
                return self.json({"sets": review.catalog(snapshot), "fetched_at": snapshot.get("fetched_at") or review.now(),
                                  "source_mode": snapshot.get("source_mode", "live")})
            except Exception:
                return self.json({"error": "최신 시트 목록을 읽지 못했습니다. 네트워크와 링크 보기 권한을 확인하세요."}, 502)
        if path == "/api/status":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            revision = params.get("after_revision", [""])[0]
            revision = int(revision) if revision.isdecimal() and len(revision) < 20 else None
            return self.json(self.server.manager.poll(params.get("after_id", [None])[0], revision, compact=True))
        match = re.fullmatch(r"/api/report/([0-9a-f]{24})\.(json|md)", path)
        if match:
            report_path = self.server.manager.output_dir / "reports" / f"{match[1]}.{match[2]}"
            if report_path.is_file():
                return self.send_bytes(report_path.read_bytes(), "application/json; charset=utf-8" if match[2] == "json" else "text/markdown; charset=utf-8", filename=f"ReadAI-review-{match[1]}.{match[2]}")
            return self.json({"error": "아직 저장된 보고서가 없습니다."}, 404)
        static = {"/": "index.html", "/styles.css": "styles.css", "/app.js": "app.js"}
        if path in static:
            file = ROOT / "web" / static[path]
            return self.send_bytes(file.read_bytes(), (mimetypes.guess_type(str(file))[0] or "text/plain") + "; charset=utf-8")
        return self.json({"error": "찾을 수 없는 주소입니다."}, 404)

    def do_POST(self):
        token = self.headers.get("X-Review-Token", "")
        if not self.allowed_host() or not self.same_origin() or not token.isascii() or not secrets.compare_digest(token, self.server.token):
            return self.json({"error": "프로그램 화면에서 실행해 주세요."}, 403)
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError("요청 길이가 올바르지 않습니다.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 16384:
                raise ValueError("요청 크기를 초과했습니다.")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("JSON 요청이 필요합니다.")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("요청 데이터가 완전하지 않습니다.")
            try:
                data = json.loads(raw or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("JSON 요청을 읽을 수 없습니다.") from None
            if not isinstance(data, dict):
                raise ValueError("설정 형식이 올바르지 않습니다.")
            if self.path == "/api/review":
                return self.json({"id": self.server.manager.start(data)}, 202)
            if self.path == "/api/cancel":
                if set(data) != {"id"} or not isinstance(data["id"], str) or not RUN_ID.fullmatch(data["id"]):
                    raise ValueError("중단할 검토 ID가 필요합니다.")
                self.server.manager.cancel(data["id"])
                return self.json({"ok": True})
            return self.json({"error": "찾을 수 없는 요청입니다."}, 404)
        except RuntimeError as exc:
            return self.json({"error": str(exc)}, 409)
        except ValueError as exc:
            return self.json({"error": str(exc)}, 400)
        except TimeoutError:
            return self.json({"error": "요청 시간이 초과되었습니다."}, 408)


class LocalServer(ThreadingHTTPServer):
    """Bound local request work; this remains a single-user desktop server."""
    def __init__(self, *args, **kwargs):
        self.request_slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        return request, address

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 2\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


def make_server(port=8769, manager=None):
    server = LocalServer(("127.0.0.1", port), Handler)
    server.manager = manager or ReviewManager()
    server.token = secrets.token_urlsafe(32)
    return server


def main():
    parser = argparse.ArgumentParser(description="리드AI 시트 검토 GUI")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    try:
        server = make_server(args.port)
    except OSError:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
                if json.load(response).get("app_id") != APP_ID:
                    raise ValueError()
            if not args.no_browser:
                webbrowser.open(url)
            print("이미 실행 중인 리드AI 검토 화면을 열었습니다.")
            return
        except Exception:
            parser.exit(1, f"{args.port} 포트에서 실행할 수 없습니다. 다른 --port 값을 사용하세요.\n")
    print(f"리드AI 시트 검토: {url}\n이 터미널을 닫으면 프로그램이 종료됩니다. 종료: Control+C", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    def terminate(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.manager.cancel()
    finally:
        server.manager.cancel()
        if server.manager.worker:
            server.manager.worker.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
