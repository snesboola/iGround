#!/bin/bash
# Double-click this file in Finder to start iGround.
cd "$(dirname "$0")" || exit 1
clear
if ! command -v python3 >/dev/null 2>&1; then
  echo "iGround needs Python 3. macOS will now offer to install it (the 'Command Line Developer Tools')."
  echo "When that's finished, double-click iGround again."
  xcode-select --install 2>/dev/null
  read -r -p "Press Return to close."
  exit 1
fi
PYTHONPATH="$PWD/src" python3 -m iground "$@"
echo
read -r -p "Press Return to close this window."
