#!/bin/sh
set -eu
LYNCEUS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$LYNCEUS_DIR"
exec .agent-venv/bin/python -u -m lynceus agent-worker "$@"
