#!/usr/bin/env bash
# Rebuild The Stacks data from the live prod DB and push it to the server.
#
# The stacks/assets/ payload (library.json + cover atlases) is generated,
# personal, and gitignored — it deploys via rsync, not git. Run this after
# library changes you want reflected at <your-domain>/stacks.
#
# Requires STACKS_DEPLOY_SERVER (ssh target, e.g. root@your-server) in the
# environment; optionally STACKS_REMOTE_DIR (default /root/mcps).
set -euo pipefail
cd "$(dirname "$0")/.."

SERVER=${STACKS_DEPLOY_SERVER:?set STACKS_DEPLOY_SERVER to the ssh target, e.g. root@your-server}
REMOTE_DIR=${STACKS_REMOTE_DIR:-/root/mcps}
SNAP=/tmp/prod-gamelib.db

echo "==> snapshotting prod DB"
ssh "$SERVER" "sqlite3 $REMOTE_DIR/data/library/gamelib.db '.backup /tmp/gamelib-snap.db'"
scp -q "$SERVER:/tmp/gamelib-snap.db" "$SNAP"
ssh "$SERVER" 'rm -f /tmp/gamelib-snap.db'

echo "==> exporting library + cover atlases"
.venv/bin/python scripts/export_stacks.py --db "$SNAP"

echo "==> rsyncing assets to server"
rsync -az --delete stacks/assets/ "$SERVER:$REMOTE_DIR/stacks/assets/"

echo "done — the site serves them at /stacks/"
