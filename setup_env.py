#!/usr/bin/env python3
"""Create a private local settings file without replacing existing values."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_KEYS = {"OPENAI_API_KEY", "OPENAI_MODEL", "READAI_ACCESS_PASSWORD"}


def ensure_env(root=ROOT):
    root = Path(root)
    target = root / ".env"
    if target.is_file():
        return target, False
    template = (root / ".env.example").read_text(encoding="utf-8")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not target.is_file():
            raise ValueError(".env 경로가 파일이 아닙니다.")
        return target, False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(template)
    return target, True


def load_env(path):
    """Read supported values as text, without executing shell expressions."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in ENV_KEYS:
            continue
        value = value.strip()
        if value[:1] in ("'", '"') and len(value) > 1 and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


if __name__ == "__main__":
    path, created = ensure_env()
    print(f"{'.env 생성 완료' if created else '기존 .env 유지'}: {path}")
    print("OPENAI_API_KEY에 키를 입력한 뒤 python web_server.py로 실행하세요.")
