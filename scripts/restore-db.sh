#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    printf 'Uso: %s data/backups/bridge-AAAAMMDDTHHMMSSZ.db\n' "$0" >&2
    exit 2
}

[[ $# -eq 1 ]] || usage

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/deploy/docker-compose.yml}"
DATA_DIR="$(realpath -e -- "${ROOT_DIR}/data")"
BACKUP_DIR="$(realpath -e -- "${ROOT_DIR}/data/backups")"
BACKUP_FILE="$(realpath -e -- "$1")"

case "${BACKUP_FILE}" in
    "${BACKUP_DIR}"/*) ;;
    *)
        printf 'O backup precisa estar dentro de %s.\n' "${BACKUP_DIR}" >&2
        exit 2
        ;;
esac

RELATIVE_BACKUP="${BACKUP_FILE#"${DATA_DIR}/"}"
CONTAINER_BACKUP="/app/data/${RELATIVE_BACKUP}"

cd "${ROOT_DIR}"

docker compose -f "${COMPOSE_FILE}" run --rm --no-deps app \
    python - "${CONTAINER_BACKUP}" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    result = connection.execute("PRAGMA integrity_check").fetchone()
finally:
    connection.close()
if result is None or result[0] != "ok":
    raise SystemExit("backup integrity check failed")
PY

printf 'ATENÇÃO: o banco ativo será substituído por %s.\n' "${RELATIVE_BACKUP}"
read -r -p 'Digite RESTAURAR para continuar: ' CONFIRMATION
if [[ "${CONFIRMATION}" != "RESTAURAR" ]]; then
    printf 'Restauração cancelada.\n'
    exit 1
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PRE_RESTORE="/app/data/backups/pre-restore-${TIMESTAMP}.db"
APP_STOPPED=false

restart_on_exit() {
    if [[ "${APP_STOPPED}" == true ]]; then
        docker compose -f "${COMPOSE_FILE}" up -d --no-build app >/dev/null
    fi
}
trap restart_on_exit EXIT

docker compose -f "${COMPOSE_FILE}" stop app
APP_STOPPED=true

docker compose -f "${COMPOSE_FILE}" run --rm --no-deps app \
    python - "${CONTAINER_BACKUP}" "${PRE_RESTORE}" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

backup_path = Path(sys.argv[1])
pre_restore_path = Path(sys.argv[2])
database_path = Path("/app/data/bridge.db")
pre_restore_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

current = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
pre_restore = sqlite3.connect(pre_restore_path)
try:
    current.backup(pre_restore)
    result = pre_restore.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise SystemExit("pre-restore backup integrity check failed")
finally:
    pre_restore.close()
    current.close()
os.chmod(pre_restore_path, 0o600)

for suffix in ("-wal", "-shm"):
    Path(f"{database_path}{suffix}").unlink(missing_ok=True)

source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
target = sqlite3.connect(database_path)
try:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise SystemExit("restored database integrity check failed")
finally:
    target.close()
    source.close()
PY

docker compose -f "${COMPOSE_FILE}" run --rm --no-deps app alembic upgrade head
docker compose -f "${COMPOSE_FILE}" up -d --no-build app
APP_STOPPED=false
trap - EXIT

printf 'Banco restaurado, migrado e serviço reiniciado.\n'
