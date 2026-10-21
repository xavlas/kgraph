#!/usr/bin/env bash
# Start the kgraph desktop app (Electron). It launches the local Python server itself.
# One-time: Node.js 18+ and Python 3.10+ ; the first run installs Electron (needs network).
#   ./run-desktop.sh            run
#   ./run-desktop.sh dist       build an installer into electron/dist (dmg / nsis / AppImage)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/electron"
command -v node >/dev/null || { echo "Node.js 18+ is required (https://nodejs.org)" >&2; exit 1; }
command -v python3 >/dev/null || command -v python >/dev/null || { echo "Python 3.10+ is required" >&2; exit 1; }
[ -d node_modules ] || npm install
case "${1:-start}" in
  dist) exec npm run dist ;;
  *)    exec npm start ;;
esac
