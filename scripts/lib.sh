# Shared by the scripts in this folder (source it): where uv is, and the schedule values from config/settings.yaml, so a
# schedule is written down once.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# systemd has no login PATH, so units need uv's absolute path: PATH first, else the copy Hermes installed
find_uv() { command -v uv || ls -d "$HOME"/.hermes/tools/uv-*/uv 2>/dev/null | sort -V | tail -1 || true; }

# schedule autopause-hours | budget-hour | plan-cron   (see metaads/schedule.py; needs $UV)
schedule() { "$UV" run --frozen --project "$REPO" python -m metaads.schedule "$1"; }
