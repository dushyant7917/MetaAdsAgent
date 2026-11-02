#!/usr/bin/env bash
# Installs the systemd timer that runs the orphan-data cleanup (metaads.orphan_data) every day at the hour in
# schedules.orphan_data_cleanup_hour_ist (config/settings.yaml), IST. It deletes the files and folders on disk that no ad
# points to. Safe to re-run; run it again after changing the schedule or switching between dry run and live.
#   scripts/install_orphan_data_cleanup_timer.sh           dry run: reports what it would delete, changes nothing
#   scripts/install_orphan_data_cleanup_timer.sh --apply   live: really deletes
# Code, config/settings.yaml and .env changes need no reinstall: every run is a fresh process that reads them.
# Re-run this script if uv moves or is upgraded (the unit stores its absolute path).
set -euo pipefail

source "$(dirname "$0")/lib.sh"
install_daily_timer metaads-orphan-data-cleanup metaads.orphan_data orphan-data-hour \
  "orphan data cleanup of files no ad points to" "deletes files for real" "${1:-}"
