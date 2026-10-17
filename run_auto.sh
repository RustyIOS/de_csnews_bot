#!/usr/bin/env bash
# cron wrapper: ./run_auto.sh announce|results [args]  -> logs/<name>_auto.log
cd "$(dirname "$0")" || exit 1
# Box cron keeps the token in .cron.env (chmod 600, never committed). Actions and a
# local dry-run pass TELEGRAM_BOT_TOKEN in the environment instead.
if [ -f .cron.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.cron.env
  set +a
fi
export PATH=/usr/local/bin:/usr/bin:/bin TZ=Europe/Moscow
mkdir -p logs
exec >> "logs/$1_auto.log" 2>&1
echo "===== $(date '+%F %T %Z') $*"
timeout 1500 python3 "$1_auto.py" "${@:2}"
echo "exit=$?"
