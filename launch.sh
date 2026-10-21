#!/usr/bin/env bash
# kgraph — single entry point.
#
#   ./launch.sh                       start the web app on the bundled example (opens the browser)
#   ./launch.sh web [run.sh options]  same, with options: ./launch.sh web ~/work/app 9000
#                                     --no-heuristics | --llm | --modernization [--modules X] | --yes | --test
#   ./launch.sh desktop               start the Electron desktop app   (./launch.sh desktop dist = build an installer)
#   ./launch.sh stop                  stop every running kgraph server (frees the ports)
#   ./launch.sh status                list running kgraph servers and their URL
#   ./launch.sh scan <repo> [out.json]   scan from the terminal, write the graph JSON (default graph.json)
#   ./launch.sh neo4j <repo|graph.json> [--load] [--clear]   Cypher script (graph.cypher) and/or load into a local Neo4j
#   ./launch.sh neo4j-up [repo|graph.json]   start a local Neo4j (Podman or Docker), load the graph into it, open Neo4j Browser
#   ./launch.sh neo4j-down [--purge]  stop and remove that Neo4j container (--purge also deletes its data volume)
#   ./launch.sh all [repo]            Neo4j + the web app, with Neo4j as the QUERY ENGINE (the app loads the graph into it)
#   ./launch.sh full [repo]           Neo4j + the Electron desktop app, Neo4j as the query engine, same repository
#                                     (already have a Neo4j?  NEO4J_PASSWORD=... [NEO4J_URI=...] ./launch.sh web   -> same, no container)
#   ./launch.sh test                  run the unit tests (Python, plus the Electron launcher tests if Node is installed)
#   ./launch.sh help
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

help_text() { awk 'NR>1 && /^set -euo/{exit} NR>1{print}' "$0"; }

NEO_NAME="kgraph-neo4j"; NEO_VOL="kgraph-neo4j-data"; NEO_IMAGE="${KGRAPH_NEO4J_IMAGE:-docker.io/library/neo4j:5}"
NEO_HTTP="http://127.0.0.1:7474"; PASS_FILE="${KGRAPH_PASS_FILE:-.kgraph/neo4j-password}"

container_cli() {   # KGRAPH_CONTAINER=podman|docker (or a path) overrides; otherwise Podman first, then Docker
  if [ -n "${KGRAPH_CONTAINER:-}" ]; then command -v "$KGRAPH_CONTAINER" >/dev/null && echo "$KGRAPH_CONTAINER"
  elif command -v podman >/dev/null; then echo podman
  elif command -v docker >/dev/null; then echo docker
  fi
}

neo4j_password() {   # NEO4J_PASSWORD if set, else a random one kept in .kgraph/ (mode 600) for this throw-away local container
  if [ -n "${NEO4J_PASSWORD:-}" ]; then printf '%s' "$NEO4J_PASSWORD"; return; fi
  if [ ! -s "$PASS_FILE" ]; then
    mkdir -p .kgraph; ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(14))' > "$PASS_FILE" )
  fi
  cat "$PASS_FILE"
}

neo4j_up() {
  need_python
  local ct src="${1:-}" pass state
  ct="$(container_cli)"
  [ -n "$ct" ] || {
    echo "Podman or Docker is required to start Neo4j for you." >&2
    echo "Otherwise install Neo4j yourself, then load with:  ./launch.sh neo4j <repo|graph.json> --load" >&2; exit 1; }
  "$ct" info >/dev/null 2>&1 || {
    echo "$ct is installed but not ready." >&2
    case "$ct" in *podman*) echo "On macOS/Windows start the Podman machine:  podman machine init   (once)   then   podman machine start" >&2 ;; esac
    exit 1; }
  pass="$(neo4j_password)"
  state="$("$ct" inspect -f '{{.State.Running}}' "$NEO_NAME" 2>/dev/null || echo none)"
  if [ "$state" = "true" ]; then echo "Neo4j container '$NEO_NAME' is already running ($ct)."
  elif [ "$state" = "false" ]; then echo "Starting the existing container '$NEO_NAME' ($ct)..."; "$ct" start "$NEO_NAME" >/dev/null
  else
    echo "Creating Neo4j container '$NEO_NAME' with $ct ($NEO_IMAGE), bound to 127.0.0.1 only..."
    # the password goes through the environment, not the command line
    NEO4J_AUTH="neo4j/$pass" "$ct" run -d --name "$NEO_NAME" -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 \
      -e NEO4J_AUTH -v "$NEO_VOL:/data" "$NEO_IMAGE" >/dev/null
  fi
  if [ "${KGRAPH_DRY_RUN:-0}" = 1 ]; then echo "(dry run: not waiting for Neo4j, not loading)"; return 0; fi
  printf "Waiting for Neo4j"
  for _ in $(seq 1 90); do curl -fs "$NEO_HTTP" >/dev/null 2>&1 && break; printf "."; sleep 1; done; echo
  curl -fs "$NEO_HTTP" >/dev/null 2>&1 || { echo "Neo4j did not answer on $NEO_HTTP (see: $ct logs $NEO_NAME)" >&2; exit 1; }
  if [ "${KGRAPH_SKIP_LOAD:-0}" = 1 ]; then      # the application synchronises the graph itself (Neo4j is its query engine)
    echo "Neo4j is ready on $NEO_HTTP (user neo4j, password in ${NEO4J_PASSWORD:+NEO4J_PASSWORD}${NEO4J_PASSWORD:-$PASS_FILE})."; return 0
  fi
  local args=(--load --clear --yes --wait 90 --uri "$NEO_HTTP" --user neo4j)
  if [ -z "$src" ]; then
    if [ -s .kgraph/last-graph.json ]; then src=".kgraph/last-graph.json"; else src="samples/shop"; fi
  fi
  case "$src" in
    *.json) NEO4J_PASSWORD="$pass" python3 -m kgraph neo4j --graph "$src" "${args[@]}" ;;
    *)      NEO4J_PASSWORD="$pass" python3 -m kgraph neo4j --repo "$src" "${args[@]}" ;;
  esac
  echo
  echo "Neo4j Browser : $NEO_HTTP    (connect with bolt://127.0.0.1:7687)"
  echo "User          : neo4j"
  echo "Password      : ${NEO4J_PASSWORD:+(from NEO4J_PASSWORD)}${NEO4J_PASSWORD:-stored in $PASS_FILE (mode 600)}"
  echo "Try           : MATCH (a)-[r]->(b) RETURN labels(a)[0] AS from, type(r) AS rel, labels(b)[0] AS to, count(*) AS n ORDER BY n DESC"
  (open "$NEO_HTTP" || xdg-open "$NEO_HTTP") >/dev/null 2>&1 || true
}

export_engine_env() {   # the app reads these to use Neo4j as its query engine (environment only: never on a command line)
  export NEO4J_URI="$NEO_HTTP" NEO4J_USER=neo4j NEO4J_PASSWORD="$(neo4j_password)" KGRAPH_ENGINE=neo4j
}

neo4j_down() {
  local ct; ct="$(container_cli)"
  [ -n "$ct" ] || { echo "Podman / Docker not found." >&2; exit 1; }
  "$ct" rm -f "$NEO_NAME" >/dev/null 2>&1 && echo "container '$NEO_NAME' removed" || echo "no container '$NEO_NAME'"
  if [ "${1:-}" = "--purge" ]; then "$ct" volume rm "$NEO_VOL" >/dev/null 2>&1 && echo "volume '$NEO_VOL' deleted" || true; rm -f "$PASS_FILE"; fi
}

need_python() {
  command -v python3 >/dev/null || { echo "python3 (>= 3.10) is required" >&2; exit 1; }
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python >= 3.10 is required" >&2; exit 1; }
}

servers() {   # "pid port" for every running kgraph server
  ps -eo pid=,args= 2>/dev/null | grep -E "kgraph serve" | grep -v grep | while read -r pid args; do
    port="$(echo "$args" | sed -n 's/.*--port[ =]\([0-9][0-9]*\).*/\1/p')"
    echo "$pid ${port:-8765}"
  done
}

cmd="${1:-web}"; [ $# -gt 0 ] && shift || true
case "$cmd" in
  web)      exec ./run.sh "$@" ;;
  desktop)  exec ./run-desktop.sh "$@" ;;
  stop)
    list="$(servers)"
    if [ -z "$list" ]; then echo "No kgraph server is running."; exit 0; fi
    echo "$list" | while read -r pid port; do kill "$pid" 2>/dev/null && echo "stopped pid $pid (port $port)" || echo "could not stop pid $pid"; done ;;
  status)
    list="$(servers)"
    if [ -z "$list" ]; then echo "No kgraph server is running."; exit 0; fi
    echo "$list" | while read -r pid port; do echo "pid $pid  http://127.0.0.1:$port"; done ;;
  scan)
    need_python
    src="${1:?usage: ./launch.sh scan <repo-path-or-git-url> [out.json]}"; out="${2:-graph.json}"
    python3 -m kgraph scan "$src" --out "$out" ;;
  neo4j)
    need_python
    src="${1:?usage: ./launch.sh neo4j <repo-path|graph.json> [--load] [--clear]}"; shift
    case "$src" in *.json) python3 -m kgraph neo4j --graph "$src" "$@" ;; *) python3 -m kgraph neo4j --repo "$src" "$@" ;; esac ;;
  neo4j-up)   neo4j_up "$@" ;;
  neo4j-down) neo4j_down "$@" ;;
  all)
    KGRAPH_SKIP_LOAD=1 neo4j_up "$@"
    if [ "${KGRAPH_DRY_RUN:-0}" = 1 ]; then export_engine_env; echo "(dry run: would start the web app with KGRAPH_ENGINE=$KGRAPH_ENGINE NEO4J_URI=$NEO4J_URI)"; exit 0; fi
    export_engine_env; exec ./run.sh "$@" ;;
  full)
    repo=""
    if [ -n "${1:-}" ] && [ "${1#-}" = "$1" ]; then
      case "$1" in
        http://*|https://*|ssh://*|git@*) repo="$1" ;;
        *) [ -d "$1" ] || { echo "not a directory: $1" >&2; exit 1; }; repo="$(cd "$1" && pwd)" ;;
      esac
    fi
    KGRAPH_SKIP_LOAD=1 neo4j_up ${repo:+"$repo"}
    export_engine_env
    if [ "${KGRAPH_DRY_RUN:-0}" = 1 ]; then echo "(dry run: would start the desktop app${repo:+ on $repo} with KGRAPH_ENGINE=$KGRAPH_ENGINE NEO4J_URI=$NEO4J_URI)"; exit 0; fi
    # the desktop app scans the same repository, so what you see in the app is what is in Neo4j
    exec env ${repo:+KGRAPH_SCAN="$repo"} ./run-desktop.sh ;;
  test)
    need_python
    python3 -m unittest discover -s tests
    if command -v node >/dev/null && [ -d electron/test ]; then ( cd electron && node --test ); fi ;;
  help|-h|--help) help_text ;;
  *) echo "unknown command '$cmd'" >&2; help_text >&2; exit 1 ;;
esac
