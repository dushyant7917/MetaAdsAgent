#!/usr/bin/env bash
# Installs the Hermes cron job that plans the next launch's ads every day at schedules.daily_plan_ist (config/settings.yaml, IST) and sends the plan to the owner on
# Telegram for approval (skill ad-daily-plan). The job only plans: nothing is made until the owner approves in the chat.
# Safe to re-run: the job of the same name is replaced. Skill, tool and config changes need no reinstall; run
# scripts/redeploy.sh after changing the skill text.
#   scripts/install_daily_plan_cron.sh           install (or replace) the job
#   scripts/install_daily_plan_cron.sh --run     also run it once right now (a real plan: reads Meta, no ads are made)
set -euo pipefail

source "$(dirname "$0")/lib.sh"
UV="$(find_uv)"
[ -n "$UV" ] && [ -x "$UV" ] || { echo "uv not found (not on PATH, not in ~/.hermes/tools)." >&2; exit 1; }
PROFILE="${PROFILE:-metaadsbot}"
JOB=daily-ad-plan
# Hermes reads a cron expression in its own timezone, which is unset here (server time, UTC), and changing it would
# move every profile's jobs; metaads.schedule turns the IST time into the equivalent UTC cron expression.
SCHEDULE="$(schedule plan-cron)"
PROMPT="Run the daily ad plan: follow the ad-daily-plan skill, part 1 (the scheduled run)."

# The owner's chat: the first Telegram user in the repo .env (the same one the finished ads are delivered to).
OWNER="$(sed -n 's/^TELEGRAM_ALLOWED_USERS=\([^,# ]*\).*/\1/p' "$REPO/.env" | head -1 | tr -d "'\"")"
[ -n "$OWNER" ] || { echo "TELEGRAM_ALLOWED_USERS is not set in $REPO/.env." >&2; exit 1; }

hermes -p "$PROFILE" cron remove "$JOB" >/dev/null 2>&1 || true
hermes -p "$PROFILE" cron create "$SCHEDULE" "$PROMPT" --name "$JOB" --skill ad-daily-plan --deliver "telegram:$OWNER"
hermes -p "$PROFILE" cron list

echo
echo "Plans arrive daily at the configured IST time (cron '$SCHEDULE' on the UTC clock) in Telegram chat $OWNER; failures are delivered there too."
echo "Logs and runs:  hermes -p $PROFILE cron runs    Pause:  hermes -p $PROFILE cron pause $JOB"
if [ "${1:-}" = "--run" ]; then
  hermes -p "$PROFILE" cron run "$JOB"
  echo "Queued: it runs on the next scheduler tick (within a minute) and the plan arrives in Telegram."
fi
