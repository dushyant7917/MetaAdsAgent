#!/usr/bin/env bash
# One command after any change: sync the Hermes profile, restart the gateway (reloads the metaads MCP server),
# then check that the tools are there. Safe to re-run.
#   scripts/redeploy.sh           refuses to restart while ads are being made or uploaded
#   scripts/redeploy.sh --force   restarts anyway (unfinished ad jobs resume; an upload must be asked for again)
set -euo pipefail

PROFILE="${PROFILE:-metaadsbot}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [ "${1:-}" != "--force" ] && [ -f "$REPO/data/metaads.db" ]; then
  busy="$(python3 - "$REPO/data/metaads.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
def count(sql):
    try:
        return c.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:          # table not created yet (fresh database)
        return 0
jobs = count("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')")
ups = count("SELECT COUNT(*) FROM meta_uploads WHERE status IN ('queued','running')")
print(f"{jobs} ad(s) being made, {ups} Meta upload(s) running" if jobs or ups else "")
PY
)"
  if [ -n "$busy" ]; then
    echo "Not restarting: $busy. Wait for them to finish, or re-run with --force." >&2
    exit 1
  fi
fi

bash "$REPO/scripts/deploy_profile.sh"
hermes gateway restart
sleep 5
hermes gateway list
hermes -p "$PROFILE" mcp test metaads | sed -n '1,6p;/upload_ads/p'

echo
echo "Telegram: send /new to the bot. A chat keeps the instructions it started with, so an existing"
echo "conversation will not see the new skill/SOUL text until you start a new session."
