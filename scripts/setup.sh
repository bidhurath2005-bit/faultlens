#!/usr/bin/env sh
# Creates .env from .env.example with random secrets (Linux/macOS/Git-Bash/CI).
set -eu
cd "$(dirname "$0")/.."
if [ -f .env ] && [ "${1:-}" != "--force" ]; then
  echo ".env already exists (use --force to regenerate)"; exit 0
fi
: > .env
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    *__GENERATE__*) secret=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n'); printf '%s\n' "$(printf '%s' "$line" | sed "s/__GENERATE__/$secret/")" >> .env ;;
    *) printf '%s\n' "$line" >> .env ;;
  esac
done < .env.example
echo "Created .env with generated local secrets."
