#!/usr/bin/env bash
# One backup of the live database. Run by a systemd timer, once a day.
#
# `sqlite3 .backup` rather than `cp`. The database runs in WAL mode, so the .db file on
# its own is missing whatever is still in the write-ahead log — and the copy looks
# perfectly fine, opens without complaint, and is quietly short of the most recent
# bookings. .backup takes the same consistent snapshot a reader would see, with the
# service still running and writing.
#
#   bash scripts/backup_db.sh [database] [destination]

set -euo pipefail

DB="${1:-/root/plumbing/data/plumbing.db}"
DEST="${2:-/root/plumbing-backups}"
KEEP_DAYS=7

[ -f "$DB" ] || { echo "no database at $DB" >&2; exit 1; }
mkdir -p "$DEST"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/plumbing-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'"

# A backup that cannot be opened is worse than no backup, because it is believed. Check
# before the old ones are deleted, so a bad snapshot never takes a good one's place.
if ! sqlite3 "$OUT" "PRAGMA integrity_check;" | grep -qx ok; then
    echo "backup failed its integrity check, keeping everything: $OUT" >&2
    exit 1
fi

ROWS="$(sqlite3 "$OUT" "SELECT COUNT(*) FROM tickets;")"
gzip -f "$OUT"
echo "$OUT.gz  ($(du -h "$OUT.gz" | cut -f1), $ROWS tickets)"

find "$DEST" -name 'plumbing-*.db.gz' -mtime "+$KEEP_DAYS" -delete
echo "kept: $(find "$DEST" -name 'plumbing-*.db.gz' | wc -l | tr -d ' ') backup(s)"
