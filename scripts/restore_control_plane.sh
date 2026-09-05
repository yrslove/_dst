#!/usr/bin/env bash
set -euo pipefail

if [[ "${RESTORE_CONFIRM:-}" != "RESTORE_DST_CONTROL_PLANE" ]]; then
  echo "Refusing restore. Set RESTORE_CONFIRM=RESTORE_DST_CONTROL_PLANE after stopping the service." >&2
  exit 2
fi
if [[ $# -ne 1 || -z "${DATABASE_URL:-}" ]]; then
  echo "Usage: DATABASE_URL=... RESTORE_CONFIRM=RESTORE_DST_CONTROL_PLANE $0 BACKUP_DIRECTORY" >&2
  exit 2
fi

backup_directory="$(realpath "$1")"
test -f "${backup_directory}/database.dump"
test -f "${backup_directory}/SHA256SUMS"
(cd "${backup_directory}" && sha256sum --check SHA256SUMS)

echo "Restoring database into explicitly supplied DATABASE_URL."
postgres_dsn="${DATABASE_URL/postgresql+psycopg:\/\//postgresql:\/\/}"
pg_restore --exit-on-error --single-transaction --clean --if-exists --no-owner --dbname="${postgres_dsn}" "${backup_directory}/database.dump"
echo "Database restore complete. Restore encryption.key separately with mode 0600, then run alembic upgrade head."
