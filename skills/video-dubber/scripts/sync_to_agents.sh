#!/bin/bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$DIR")"
TARGET_DIR="${VIDEO_DUBBER_AGENT_DIR:-$HOME/.agents/skills/video-dubber}"

if [ ! -d "$TARGET_DIR" ]; then
  echo "[SYNC] Target skill directory does not exist: $TARGET_DIR" >&2
  exit 1
fi

echo "[SYNC] Source: $SKILL_DIR"
echo "[SYNC] Target: $TARGET_DIR"

rsync -a \
  --delete \
  --exclude ".git/" \
  --exclude ".venv/" \
  --exclude ".agent/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".DS_Store" \
  "$SKILL_DIR"/ \
  "$TARGET_DIR"/

echo "[SYNC] Skill source synced to $TARGET_DIR"
echo "[SYNC] Runtime assets remain in $TARGET_DIR/.agent and environment remains in $TARGET_DIR/.venv"
