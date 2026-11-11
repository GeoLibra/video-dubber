#!/bin/bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$DIR")"
TARGET_DIR="${VIDEO_DUBBER_AGENT_DIR:-$HOME/.agents/skills/video-dubber}"

MODE="${1:-sync}"
case "$MODE" in
  sync|--dry-run|--check) ;;
  *) echo "Usage: $0 [--dry-run|--check]" >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [--dry-run|--check]" >&2
  exit 2
fi

if [ ! -d "$TARGET_DIR" ]; then
  echo "[SYNC] Target skill directory does not exist: $TARGET_DIR" >&2
  exit 1
fi

echo "[SYNC] Source: $SKILL_DIR"
echo "[SYNC] Target: $TARGET_DIR"

SYNC_ARGS=(
  -a --checksum --itemize-changes
  --include ".env.example"
  --delete \
  --exclude ".git/" \
  --exclude ".venv/" \
  --exclude ".venv-speaker/" \
  --exclude ".agent/" \
  --exclude ".env" \
  --exclude ".env.*" \
  --exclude ".pytest_cache/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".DS_Store"
)
# Include rules precede excludes because rsync uses the first matching rule.
if [ "$MODE" = "--dry-run" ]; then
  rsync "${SYNC_ARGS[@]}" --dry-run "$SKILL_DIR"/ "$TARGET_DIR"/
  exit 0
fi
if [ "$MODE" = "--check" ]; then
  CHANGES="$(rsync "${SYNC_ARGS[@]}" --dry-run "$SKILL_DIR"/ "$TARGET_DIR"/)"
  if [ -n "$CHANGES" ]; then
    printf '%s\n' "$CHANGES"
    echo "[SYNC] Source and target differ." >&2
    exit 1
  fi
  echo "[SYNC] Source and target content match."
  exit 0
fi
rsync "${SYNC_ARGS[@]}" "$SKILL_DIR"/ "$TARGET_DIR"/

echo "[SYNC] Skill source synced to $TARGET_DIR"
echo "[SYNC] Runtime assets remain in $TARGET_DIR/.agent and environments remain in $TARGET_DIR/.venv and $TARGET_DIR/.venv-speaker"
