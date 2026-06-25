#!/bin/bash
set -e

echo "🎙️ Installing Video Dubber Skill..."

# Make sure the setup script is executable
chmod +x ./skills/video-dubber/scripts/setup_env.sh

# Execute the setup script to create the environment and install dependencies
./skills/video-dubber/scripts/setup_env.sh

echo "======================================"
echo "✅ Environment setup complete."
echo "To add this skill to your agent, run:"
echo "  npx skills add ./skills/video-dubber -a codex -g"
