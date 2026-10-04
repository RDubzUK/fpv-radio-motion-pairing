#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' 'Install the environment first: uv sync --frozen'
  exit 1
fi
exec .venv/bin/python -m fpv_matcher.web --host 0.0.0.0 --port 8767 "$@"
