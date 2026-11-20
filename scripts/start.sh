#!/bin/sh
set -eu
LYNCEUS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$LYNCEUS_DIR"
export LYNCEUS_UID=$(id -u) LYNCEUS_GID=$(id -g)
mkdir -p var
docker compose up -d --build
