#!/usr/bin/env bash
# Installs the systemd timer that runs the old-data cleanup (metaads.old_data) every day at the hour in
# schedules.old_data_cleanup_hour_ist (config/settings.yaml), IST. It deletes the files of ads that were uploaded to
# Meta, dropped or failed long enough ago (old_data in config/settings.yaml), except the work folders of winning hooks that
# can still be reused. It reads Meta once. Safe to re-run; run it again after changing the schedule or switching between
# dry run and live.
#   scripts/install_old_data_cleanup_timer.sh           dry run: reports what it would delete, changes nothing
#   scripts/install_old_data_cleanup_timer.sh --apply   live: really deletes
# Code, config/settings.yaml and .env changes need no reinstall: every run is a fresh process that reads them.
# Re-run this script if uv moves or is upgraded (the unit stores its absolute path).
set -euo pipefail

source "$(dirname "$0")/lib.sh"
install_daily_timer metaads-old-data-cleanup metaads.old_data old-data-hour \
  "old data cleanup of files of ads that are done with" "deletes files for real" "${1:-}"
