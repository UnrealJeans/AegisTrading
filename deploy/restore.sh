#!/usr/bin/env bash
# Restore a dump created by the backup service. Stops everything that writes to the database first.
#   ./deploy/restore.sh backups/aegis-20260101T000000Z.dump
set -euo pipefail
FILE="${1:?usage: deploy/restore.sh <backups/aegis-....dump>}"
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 1; }
read -r -p "This REPLACES the current database with $FILE. Type 'restore' to continue: " answer
[ "$answer" = "restore" ] || { echo "aborted"; exit 1; }
docker compose stop backend worker orchestrator trading-engine
docker compose exec -T postgres pg_restore -U aegis -d aegis --clean --if-exists --no-owner < "$FILE"
docker compose up -d
echo "restore complete. Trading status is whatever the dump contained; check the dashboard before resuming."
