#!/bin/sh
# Periodic compressed PostgreSQL dumps with retention. Runs inside the `backup` container.
set -eu
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
mkdir -p /backups
while true; do
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="/backups/.aegis-${ts}.dump.tmp"
  if pg_dump -h postgres -U aegis -d aegis -Fc -Z 6 -f "$tmp"; then
    mv "$tmp" "/backups/aegis-${ts}.dump"
    echo "{\"event\":\"backup_ok\",\"file\":\"aegis-${ts}.dump\",\"bytes\":$(wc -c < "/backups/aegis-${ts}.dump")}"
    find /backups -name 'aegis-*.dump' -mtime "+${KEEP_DAYS}" -delete
  else
    rm -f "$tmp"
    echo "{\"event\":\"backup_failed\",\"ts\":\"${ts}\"}" >&2
  fi
  sleep "$INTERVAL"
done
