#!/usr/bin/env bash
set -euo pipefail
SRC=/opt/chores/app/data
DEST=/opt/chores/backups
STAMP=$(date +%F-%H%M)
mkdir -p "$DEST"
sqlite3 "$SRC/chores.db" ".backup '$DEST/chores-$STAMP.db'"
tar -czf "$DEST/photos-$STAMP.tar.gz" -C "$SRC" photos
ls -1t "$DEST"/chores-*.db | tail -n +15 | xargs -r rm
ls -1t "$DEST"/photos-*.tar.gz | tail -n +15 | xargs -r rm
