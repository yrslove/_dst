#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ -z "${DATABASE_URL:-}" || -z "${DST_FARM_SECRET_KEY_FILE:-}" ]]; then
  echo "DATABASE_URL and DST_FARM_SECRET_KEY_FILE are required" >&2
  exit 2
fi
if [[ ! -f "${DST_FARM_SECRET_KEY_FILE}" ]]; then
  echo "Encryption key file is missing" >&2
  exit 2
fi

backup_root="${BACKUP_ROOT:-/var/backups/dst-orchestrator}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="${backup_root}/${timestamp}"
install -d -m 0700 "${destination}"

postgres_dsn="${DATABASE_URL/postgresql+psycopg:\/\//postgresql:\/\/}"
pg_dump --format=custom --no-owner --file="${destination}/database.dump" "${postgres_dsn}"
install -m 0600 "${DST_FARM_SECRET_KEY_FILE}" "${destination}/encryption.key"
if [[ -n "${CONTROL_PLANE_ENV_FILE:-}" && -f "${CONTROL_PLANE_ENV_FILE}" ]]; then
  install -m 0600 "${CONTROL_PLANE_ENV_FILE}" "${destination}/control-plane.env"
fi
(cd "${destination}" && sha256sum database.dump encryption.key > SHA256SUMS
 if [[ -f control-plane.env ]]; then sha256sum control-plane.env >> SHA256SUMS; fi)
echo "Backup created at ${destination}; move an encrypted copy off-host."
