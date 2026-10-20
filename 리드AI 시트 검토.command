#!/bin/zsh
set -e
TASK_DIR="${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$TASK_DIR"
if ! command -v python3 >/dev/null 2>&1; then
  print 'Python 3.11 이상이 필요합니다.'
  read 'reply?Enter 키를 누르면 닫습니다. '
  exit 1
fi
exec python3 "$TASK_DIR/app.py"
