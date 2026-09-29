#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/deploy/docker-compose.yml}"

cd "${ROOT_DIR}"

docker compose -f "${COMPOSE_FILE}" config --quiet
docker compose -f "${COMPOSE_FILE}" ps
APP_UID="$(docker compose -f "${COMPOSE_FILE}" exec -T app id -u | tr -d '\r')"
if [[ "${APP_UID}" == "0" ]]; then
    printf 'Falha: o container da aplicação está rodando como root.\n' >&2
    exit 1
fi
docker compose -f "${COMPOSE_FILE}" exec -T app python - <<'PY'
import json
import stat
import urllib.request
from pathlib import Path

for endpoint, expected in (("health", "ok"), ("ready", "ready")):
    with urllib.request.urlopen(f"http://127.0.0.1:8000/{endpoint}", timeout=5) as response:
        payload = json.load(response)
    if payload.get("status") != expected:
        raise SystemExit(f"{endpoint} returned an unexpected status")

expected_modes = {Path("/app/data"): 0o700, Path("/app/data/bridge.db"): 0o600}
for path, expected_mode in expected_modes.items():
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    if actual_mode != expected_mode:
        raise SystemExit(f"{path} mode is {actual_mode:o}; expected {expected_mode:o}")
PY
docker compose -f "${COMPOSE_FILE}" exec -T app alembic current

printf 'OLX Telegram Bridge: health, readiness, migration e usuário não-root OK.\n'
