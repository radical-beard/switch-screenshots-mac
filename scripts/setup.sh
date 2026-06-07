#!/usr/bin/env bash
# Install dependencies for the Switch screenshot transfer tool.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required. Install it first: https://brew.sh" >&2
  exit 1
fi

if command -v gphoto2 >/dev/null 2>&1; then
  echo "gphoto2 already installed ($(gphoto2 --version 2>/dev/null | head -1))"
else
  echo "Installing gphoto2 ..."
  brew install gphoto2
fi

echo
echo "Setup complete."
echo "On the console: Settings -> Data Management -> Manage Screenshots and Videos"
echo "  -> 'Copy to PC over USB'  (bottom USB-C port, not the dock), then run:  breach run"
