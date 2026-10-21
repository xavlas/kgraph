#!/usr/bin/env bash
# Start a local Neo4j (Podman or Docker), load the graph into it, then open the Electron desktop app on the same repository.
#   ./run-desktop-neo4j.sh [repo-path-or-git-url]      (no argument: the last scanned graph, or the bundled example)
#   ./launch.sh neo4j-down                             to stop Neo4j afterwards
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec ./launch.sh full "$@"
