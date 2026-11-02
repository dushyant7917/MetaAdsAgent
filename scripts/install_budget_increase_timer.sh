#!/usr/bin/env bash
# Installs the systemd timer that runs the budget-increase job (metaads.budget) every day at the hour in
# schedules.budget_increase_hour_ist (config/settings.yaml), IST. Keep it off the auto-pause runs so their Meta calls
# never overlap. Safe to re-run; run it again after changing the schedule or switching between dry run and live.
#   scripts/install_budget_increase_timer.sh           dry run: reports what it would raise, changes nothing
#   scripts/install_budget_increase_timer.sh --apply   live: really raises budgets
# Code, config/settings.yaml and .env changes need no reinstall: every run is a fresh process that reads them.
# Re-run this script if uv moves or is upgraded (the unit stores its absolute path).
set -euo pipefail

source "$(dirname "$0")/lib.sh"
install_daily_timer metaads-budget-increase metaads.budget budget-hour \
  "budget increase of well-performing Meta ad sets" "raises budgets for real" "${1:-}"
