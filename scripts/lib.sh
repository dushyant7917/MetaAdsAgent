# Shared by the scripts in this folder (source it): where uv is, and the schedule values from config/settings.yaml, so a
# schedule is written down once.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# systemd has no login PATH, so units need uv's absolute path: PATH first, else the copy Hermes installed
find_uv() { command -v uv || ls -d "$HOME"/.hermes/tools/uv-*/uv 2>/dev/null | sort -V | tail -1 || true; }

# schedule autopause-hours | budget-hour | orphan-data-hour | old-data-hour | plan-cron   (see metaads/schedule.py; needs $UV)
schedule() { "$UV" run --frozen --project "$REPO" python -m metaads.schedule "$1"; }

# install_daily_timer UNIT MODULE HOUR_ANSWER WHAT LIVE_MODE [--apply]
# Installs the systemd service + timer that runs `python -m MODULE` once a day at the hour `metaads.schedule
# HOUR_ANSWER` prints (IST). WHAT says in the unit description what the job does, LIVE_MODE what --apply makes it do.
# Without --apply the job only reports (dry run). Safe to re-run; run it as root.
install_daily_timer() {
  local unit="$1" module="$2" hour_answer="$3" what="$4" live_mode="$5" args="" mode hour
  mode="dry run (changes nothing)"
  if [ "${6:-}" = "--apply" ]; then args=" --apply"; mode="live ($live_mode)"; fi

  UV="$(find_uv)"
  [ -n "$UV" ] && [ -x "$UV" ] || { echo "uv not found (not on PATH, not in ~/.hermes/tools)." >&2; exit 1; }
  [ "$(id -u)" = 0 ] || { echo "Run as root (writes /etc/systemd/system)." >&2; exit 1; }
  hour="$(printf '%02d' "$(schedule "$hour_answer")")"

  cat > "/etc/systemd/system/$unit.service" <<UNIT
[Unit]
Description=metaads $what, $mode
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO
ExecStart=$UV run --frozen python -m $module$args
TimeoutStartSec=600
UNIT

  cat > "/etc/systemd/system/$unit.timer" <<UNIT
[Unit]
Description=Run $unit daily at $hour:00 IST

[Timer]
OnCalendar=*-*-* $hour:00:00 Asia/Kolkata
AccuracySec=1s
Persistent=true

[Install]
WantedBy=timers.target
UNIT

  systemctl daemon-reload
  systemctl enable --now "$unit.timer"
  systemctl list-timers "$unit.timer" --no-pager
  echo
  echo "Mode: $mode"
  echo "Run now:  systemctl start $unit.service    Logs: journalctl -u $unit -n 50 --no-pager"
}
