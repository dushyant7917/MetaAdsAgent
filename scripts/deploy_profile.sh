#!/usr/bin/env bash
# Create/refresh the Hermes profile "metaadsbot" from the files in this repo.
# Safe to re-run. Does not touch the default ~/.hermes home and never writes API keys.
set -euo pipefail

PROFILE="${PROFILE:-metaadsbot}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE"
UV="$(ls "$HOME"/.hermes/tools/uv-*/uv | head -n1)"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.7-flash}"

if [ ! -d "$PROFILE_HOME" ]; then
  hermes profile create "$PROFILE"
fi

# Gemini as the agent's own LLM (key goes in the profile .env as GEMINI_API_KEY).
hermes -p "$PROFILE" config set model.provider gemini
hermes -p "$PROFILE" config set model.default "$GEMINI_MODEL"
hermes -p "$PROFILE" config set model.base_url https://generativelanguage.googleapis.com/v1beta

# Role and skill from the repo.
install -m 600 "$REPO/hermes/SOUL.md" "$PROFILE_HOME/SOUL.md"
mkdir -p "$PROFILE_HOME/skills/ad-hook-creator"
install -m 644 "$REPO/hermes/skills/ad-hook-creator/SKILL.md" "$PROFILE_HOME/skills/ad-hook-creator/SKILL.md"

# The adhook MCP server (runs from this repo with uv; reads its keys from the profile .env).
if ! grep -q '^  adhook:' "$PROFILE_HOME/config.yaml" 2>/dev/null; then
  # 'mcp add' asks "Enable all tools? [Y/n/select]"; answer Y so this also works without a terminal.
  printf 'Y\n' | hermes -p "$PROFILE" mcp add adhook --command "$UV" \
    --env "ADHOOK_ENV_FILE=$REPO/.env" \
    --args run --project "$REPO" python -m adhook.server
fi

# Keys: $REPO/.env (gitignored) is the one place you edit. Hermes itself reads Gemini and Telegram
# settings from the profile's .env, so copy just those keys over (only the ones that have a value).
python3 - "$REPO/.env" "$PROFILE_HOME/.env" <<'PY'
import sys, pathlib
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
KEYS = ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL")
vals = {}
for line in src.read_text().splitlines() if src.exists() else []:
    k, sep, v = line.partition("=")
    v = v.split(" #")[0].strip().strip("'\"")
    if sep and k.strip() in KEYS and v:
        vals[k.strip()] = v
lines = dst.read_text().splitlines() if dst.exists() else []
# A home channel removed from the repo .env must go from the profile too (a stale one sends alerts nowhere).
drop = set(vals)
if "TELEGRAM_HOME_CHANNEL" not in vals:
    drop |= {"TELEGRAM_HOME_CHANNEL", "TELEGRAM_HOME_CHANNEL_THREAD_ID"}
lines = [l for l in lines if l.partition("=")[0].strip() not in drop]
lines += [f"{k}={v}" for k, v in vals.items()]
dst.write_text("\n".join(lines) + "\n")
dst.chmod(0o600)
print("Synced to profile .env:", ", ".join(vals) or "nothing (fill in the repo .env first)")
PY

echo "Profile '$PROFILE' ready at $PROFILE_HOME"
echo "Keys live in $REPO/.env (gitignored); re-run this script after changing them."
