#!/usr/bin/env bash
# Rebuild The Stacks data from the live prod DB and push it to the server.
#
# The stacks/assets/ payload (library.json + cover atlases) is generated,
# personal, and gitignored — it deploys via rsync, not git. Run this after
# library changes you want reflected under /stacks/ on the server.
#
# GAMELIB_DEPLOY_HOST=user@host selects the server (required); GAMELIB_DEPLOY_DIR
# is the repo clone on it, relative to the login home (default gamelib-mcp).
set -euo pipefail
cd "$(dirname "$0")/.."

SERVER="${GAMELIB_DEPLOY_HOST:?set GAMELIB_DEPLOY_HOST=user@host}"
REMOTE_DIR="${GAMELIB_DEPLOY_DIR:-gamelib-mcp}"
SNAP=/tmp/prod-gamelib.db

echo "==> snapshotting prod DB"
ssh "$SERVER" "sqlite3 $(printf %q "$REMOTE_DIR/data/library/gamelib.db") '.backup /tmp/gamelib-snap.db'"
scp -q "$SERVER:/tmp/gamelib-snap.db" "$SNAP"
ssh "$SERVER" 'rm -f /tmp/gamelib-snap.db'

echo "==> exporting library + cover atlases"
.venv/bin/python scripts/export_stacks.py --db "$SNAP"

echo "==> rsyncing assets to server"
rsync -az --delete stacks/assets/ "$SERVER:$REMOTE_DIR/stacks/assets/"

echo "done — the site serves the new assets under /stacks/"
