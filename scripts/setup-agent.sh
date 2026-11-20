#!/bin/sh
set -eu
LYNCEUS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$LYNCEUS_DIR"
if [ ! -x .agent-venv/bin/python ]; then uv venv --python 3.12 .agent-venv; fi
uv pip install --python .agent-venv/bin/python -r requirements-agent.txt
