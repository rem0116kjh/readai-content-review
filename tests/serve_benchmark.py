#!/usr/bin/env python3
"""Isolated browser benchmark fixture. No Google requests, writes, or AI calls."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    fixture = {"token": "benchmark-only", "ai": {"ready": True, "message": "로컬 성능 시험 · AI 호출 없음"},
               "source_url": report["source_url"], "saved_sources": [], "last_report": report,
               "criteria": {"version": "benchmark", "sha256": report.get("criteria_sha256"), "text": "동일 입력 성능 시험"}}
    instrumentation = """
window.__bench = {};
const benchData = document.createElement('pre');
benchData.id = 'qa-benchmark-data'; benchData.hidden = true;
document.body.append(benchData);
for (const name of ['renderResults', 'renderReport', 'renderCoverage']) {
  const original = ({renderResults, renderReport, renderCoverage})[name];
  const measured = (...args) => {
    const t = performance.now();
    try { return original(...args); }
    finally {
      (window.__bench[name] ||= []).push(performance.now() - t);
      benchData.textContent = JSON.stringify(window.__bench);
    }
  };
  if (name === 'renderResults') renderResults = measured;
  if (name === 'renderReport') renderReport = measured;
  if (name === 'renderCoverage') renderCoverage = measured;
}
"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            kind = "application/json"
            if path == "/api/bootstrap":
                value = json.dumps(fixture, ensure_ascii=False)
            elif path == "/api/status":
                value = '{"job":null}'
            elif path == "/api/catalog":
                value = json.dumps({"sets": [], "fetched_at": report["started_at"]})
            elif path in ("/", "/app.js", "/styles.css"):
                name = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}[path]
                value = (args.source / "web" / name).read_text()
                kind = {"/": "text/html", "/app.js": "text/javascript", "/styles.css": "text/css"}[path]
                if path == "/app.js":
                    value += instrumentation
            else:
                self.send_error(404)
                return
            body = value.encode()
            self.send_response(200)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Benchmark fixture: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
