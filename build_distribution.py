#!/usr/bin/env python3
"""Build a source-only desktop bundle from an explicit allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = (
    "app.py", "review.py", "criteria.py", "ai-schema.json", "ai-recheck-schema.json",
    "검수기준.md", "검수_추가기준_원문.txt", "리드AI 시트 검토.command",
    "web/app.js", "web/index.html", "web/styles.css", ".gitignore",
)


def build(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    contents = {name: (ROOT / name).read_bytes() for name in FILES}
    contents["README.md"] = (ROOT / "배포_안내.md").read_bytes()
    manifest = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
    contents["MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in contents.items():
            info = zipfile.ZipInfo("ReadAI-review/" + name, date_time=(2026, 10, 20, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith(".command") else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n")
    return {"path": str(output.resolve()), "sha256": digest, "files": len(contents), "bytes": output.stat().st_size}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".local/dist/ReadAI-review-local.zip")
    print(json.dumps(build(parser.parse_args().output), ensure_ascii=False))
