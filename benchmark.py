#!/usr/bin/env python3
"""Offline same-input timings; reports fingerprints, never sends data to AI/Sheets."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-job", action="store_true", help="Also time report persistence; uses only temporary output folders")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    import review
    snapshot = json.loads(args.snapshot.read_text())
    raw_snapshot = snapshot
    # Same per-run preparation as the application, if supported by this version.
    import criteria
    if hasattr(criteria, "prepare"):
        snapshot = criteria.prepare(snapshot)
    items = review.catalog(snapshot)

    def prompts():
        return [review.ai_prompt(snapshot, i) for i in items]

    def rules():
        return [review.rule_review(snapshot, i) for i in items]

    metrics = {}
    for name, action in (("prompt_build", prompts), ("rule_review", rules)):
        for _ in range(5):
            action()
        samples = []
        for _ in range(25):
            start = time.perf_counter()
            action()
            samples.append((time.perf_counter() - start) * 1000)
        metrics[name] = {"median_ms": statistics.median(samples), "p95_ms": sorted(samples)[23], "samples_ms": samples}
    if args.include_job:
        import app
        samples, writes = [], []
        for iteration in range(8):
            with tempfile.TemporaryDirectory(prefix="readai-benchmark-") as folder:
                manager = app.ReviewManager(folder, downloader=lambda *a: raw_snapshot)
                count = [0]
                persist = manager.persist
                def counted_persist():
                    count[0] += 1
                    persist()
                manager.persist = counted_persist
                start = time.perf_counter()
                manager.start({"grades": [1, 2, 3, 4, 5, 6], "include_sample": True, "ai": False})
                manager.worker.join(15)
                elapsed = (time.perf_counter() - start) * 1000
                assert not manager.worker.is_alive()
                job = manager.current()
                assert job["status"] == "complete" and job["completed_sets"] == len(items)
                assert job["issues"] == [finding for group in rules() for finding in group]
                if iteration:
                    samples.append(elapsed)
                    writes.append(count[0])
        metrics["rule_job_with_persistence"] = {"median_ms": statistics.median(samples), "p95_ms": max(samples),
                                                  "samples_ms": samples, "report_save_count": writes}
    payloads = prompts()
    contexts = [json.loads(p.split("[검토 데이터]\n", 1)[1]) for p in payloads]
    result = {"sets": len(items), "warmups": 5, "repetitions": 25, "metrics": metrics,
              "prompt_utf8_bytes": sum(len(p.encode()) for p in payloads),
              "input_context_sha256": fingerprint(contexts), "rule_results_sha256": fingerprint(rules()),
              "scope": "Local CPU and payload size only; excludes AI/network latency"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps({k: v for k, v in result.items() if k != "metrics"}, ensure_ascii=False))
    print(json.dumps({name: {k: v for k, v in value.items() if k != "samples_ms"} for name, value in metrics.items()}))


if __name__ == "__main__":
    main()
