#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SKILL_DIR="$(dirname "$DIR")"
cd "$SKILL_DIR"

if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$PATH"
fi

TTS_BACKEND="${VIDEO_DUBBER_TTS_BACKEND:-qwen3}"

# compute hash of all requirements files + backend choice as cache key
HASH_SOURCE=$(cat requirements.txt requirements-qwen3-tts.txt requirements-f5-mlx.txt requirements-f5-pytorch.txt 2>/dev/null)
ENV_HASH=$(echo "$HASH_SOURCE" | md5 2>/dev/null || echo "$HASH_SOURCE" | md5sum 2>/dev/null | cut -d' ' -f1)
ENV_HASH="${ENV_HASH}:${TTS_BACKEND}"

# reuse existing .venv if hash matches and key packages are importable
if [ -f .venv/.env_hash ] && [ "$(cat .venv/.env_hash)" = "$ENV_HASH" ] && [ -f .venv/bin/activate ]; then
    echo "[SETUP] .venv is up to date. Activate with: source .venv/bin/activate"
    exit 0
fi

echo "[SETUP] Creating or updating uv virtual environment..."
uv venv .venv --python 3.10 --seed
source .venv/bin/activate

echo "[SETUP] Installing dependencies..."
uv pip install -r requirements.txt

case "$TTS_BACKEND" in
  qwen3)
    echo "[SETUP] Installing Qwen3-TTS MLX backend..."
    uv pip install -r requirements-qwen3-tts.txt
    ;;
  mlx)
    echo "[SETUP] Installing MLX F5-TTS backend..."
    uv pip install -r requirements-f5-mlx.txt
    ;;
  pytorch)
    echo "[SETUP] Installing PyTorch F5-TTS backend..."
    uv pip install -r requirements-f5-pytorch.txt
    ;;
  none)
    echo "[SETUP] Skipping TTS backend install."
    ;;
  *)
    echo "Unknown VIDEO_DUBBER_TTS_BACKEND=$TTS_BACKEND. Use qwen3, mlx, pytorch, or none." >&2
    exit 1
    ;;
esac

echo "$ENV_HASH" > .venv/.env_hash
echo "[SETUP] Environment setup complete. Use 'source .venv/bin/activate' to activate."
