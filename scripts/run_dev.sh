#!/usr/bin/env bash
# Development runner (from project root)
set -euo pipefail
cd "$(dirname "$0")/.."
export DATA_DIR="${DATA_DIR:-$(pwd)/data}"
export LOG_DIR="${LOG_DIR:-$(pwd)/logs}"
mkdir -p "$DATA_DIR"/{users,catalog,jobs,results,temp} "$LOG_DIR"
if [[ ! -f .env ]]; then
  echo "Copy .env.example to .env and configure BOT_TOKEN + ADMIN_TELEGRAM_IDS"
  exit 1
fi
exec python -m app.main
