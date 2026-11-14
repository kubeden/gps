#!/bin/sh
# macOS / Linux: link the addon into WoW Forever and prepare the helper environment.
set -e
cd "$(dirname "$0")"
WOW_DIR="${WOW_DIR:-/Applications/World of Warcraft/_forever_}"
ADDONS="$WOW_DIR/Interface/AddOns"
mkdir -p "$ADDONS"
# A real copy, not a symlink: WoW Forever did not load SavedVariables for a symlinked addon folder.
[ -L "$ADDONS/GamepadSpeak" ] && rm "$ADDONS/GamepadSpeak"
rm -rf "$ADDONS/GamepadSpeak"
cp -R "$PWD/addon/GamepadSpeak" "$ADDONS/GamepadSpeak"
echo "Addon copied: $ADDONS/GamepadSpeak (re-run ./install.sh after editing the addon)"
if command -v uv >/dev/null 2>&1; then
  (cd helper && uv sync --quiet)
  echo "Helper environment ready (uv)."
else
  echo "uv not found; ./run-helper.sh will create a venv with pip on first run."
fi
echo "Next: in game run /gps setup, then ./run-helper.sh"
