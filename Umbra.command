#!/bin/zsh
# Double-click to start Umbra. It runs from the first EXOTIC environment found and keeps the Mac
# awake while it works. Close this window, or press Ctrl-C, to stop it.
cd "$(dirname "$0")"
for home in "$UMBRA_ENV" ~/exotic-new ~/exotic-env; do
  if [ -n "$home" ] && [ -x "$home/bin/python" ]; then
    exec caffeinate -i "$home/bin/python" -m umbra "$@"
  fi
done
echo "No Python environment found. Looked in ~/exotic-new and ~/exotic-env; set UMBRA_ENV to use another."
read -k 1 "?Press any key to close."
