#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/deploy/docker-compose.yml}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_NAME="bridge-${TIMESTAMP}.db"
CONTAINER_BACKUP="/app/data/backups/${BACKUP_NAME}"

cd "${ROOT_DIR}"

docker compose -f "${COMPOSE_FILE}" exec -T app python - "${CONTAINER_BACKUP}" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

destination = Path(sys.argv[1])
destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
source = sqlite3.connect("file:/app/data/bridge.db?mode=ro", uri=True)
target = sqlite3.connect(destination)
try:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise SystemExit("backup integrity check failed")
finally:
    target.close()
    source.close()
os.chmod(destination, 0o600)
PY

printf 'Backup SQLite íntegro criado em data/backups/%s\n' "${BACKUP_NAME}"
