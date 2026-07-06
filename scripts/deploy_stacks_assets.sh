#!/usr/bin/env bash
# Rebuild The Stacks data from the live prod DB and push it to the server.
#
# The stacks/assets/ payload (library.json + cover atlases) is generated,
# personal, and gitignored — it deploys via rsync, not git. Run this after
# library changes you want reflected at gamelibmcp.johnwilkos.com/stacks.
set -euo pipefail
cd "$(dirname "$0")/.."

SERVER=root@178.104.53.83
SNAP=/tmp/prod-gamelib.db

echo "==> snapshotting prod DB"
ssh "$SERVER" 'sqlite3 /root/mcps/data/library/gamelib.db ".backup /tmp/gamelib-snap.db"'
scp -q "$SERVER:/tmp/gamelib-snap.db" "$SNAP"
ssh "$SERVER" 'rm -f /tmp/gamelib-snap.db'

echo "==> exporting library + cover atlases"
.venv/bin/python scripts/export_stacks.py --db "$SNAP"

echo "==> rsyncing assets to server"
rsync -az --delete stacks/assets/ "$SERVER:/root/mcps/stacks/assets/"

echo "done — https://gamelibmcp.johnwilkos.com/stacks/"
