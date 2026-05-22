#!/usr/bin/env bash
set -euo pipefail

DUMP=/docker-entrypoint-initdb.d/00_dump.dump
DB=${POSTGRES_DB:-appdb}
USER=${POSTGRES_USER:-postgres}

if [ ! -s "$DUMP" ]; then
  echo "no dump found at $DUMP; skipping restore" >&2
  exit 0
fi

# pg_restore handles custom-format dumps; --clean --if-exists makes it idempotent
# even though postgres initdb has already created an empty $POSTGRES_DB.
pg_restore \
  --username="$USER" \
  --dbname="$DB" \
  --no-owner --no-acl \
  --clean --if-exists \
  --exit-on-error \
  "$DUMP"
