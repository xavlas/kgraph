#!/usr/bin/env bash
# Launch the knowledge-graph explorer.
# Usage: ./run.sh [--no-heuristics | --llm | --modernization [--modules PATTERN]] [--yes] [repo-path-or-git-url] [port]
#   ./run.sh                         -> scans the bundled example (samples/shop)
#   ./run.sh ~/work/my-app 9000      -> scans your repository, serves on port 9000
#   ./run.sh --no-heuristics ~/work/my-app
#                                    -> scans the STRUCTURE only (no rules); then click the
#                                       "Analyze business rules" button in the UI to run the
#                                       code-modernization plugin on demand (modules, reuse, cancel)
#   ./run.sh --llm ~/work/my-app     -> also enriches business rules with Claude
#                                       (SENDS business-class source code to Anthropic)
#   ./run.sh --modernization --modules order ~/work/my-app
#                                    -> business rules come ONLY from the Claude Code `code-modernization`
#                                       plugin (no heuristics). Many agents, long run, sends code to Anthropic.
#                                       One-time setup: claude plugin install code-modernization@claude-plugins-official
#   --yes  skips the confirmation prompts of --llm / --modernization (non-interactive use)
#   --test runs the unit tests before starting (skipped by default for a fast start)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

LLM=0; YES=0; MOD=0; NOH=0; TEST=0; MODULES=""; POS=()
while [ $# -gt 0 ]; do
  a="$1"; shift
  case "$a" in
    --llm) LLM=1 ;;
    --modernization) MOD=1 ;;
    --no-heuristics) NOH=1 ;;
    --modules) MODULES="${1:?--modules needs a value}"; shift ;;
    --yes|-y) YES=1 ;;
    --test) TEST=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) POS+=("$a") ;;
  esac
done
SOURCE="${POS[0]:-samples/shop}"
PORT="${POS[1]:-8765}"

# first free port from the requested one (an old kgraph server may still hold it)
FREE_PORT="$(python3 - "$PORT" <<'PY'
import socket, sys
start = int(sys.argv[1])
for p in range(start, start + 50):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", p)); s.close(); print(p); break
    except OSError:
        s.close()
PY
)"
if [ -z "$FREE_PORT" ]; then echo "no free port found from $PORT" >&2; exit 1; fi
if [ "$FREE_PORT" != "$PORT" ]; then
  echo "Port $PORT is already in use (probably an older kgraph server)."
  echo "  to stop it:  lsof -ti tcp:$PORT | xargs kill"
  echo "  using port $FREE_PORT instead."
  PORT="$FREE_PORT"
fi

command -v python3 >/dev/null || { echo "python3 is required (>= 3.10)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python >= 3.10 is required" >&2; exit 1; }
case "$SOURCE" in
  http://*|https://*|ssh://*|git@*) command -v git >/dev/null || { echo "git is required for URLs" >&2; exit 1; } ;;
  *) [ -d "$SOURCE" ] || { echo "not a directory: $SOURCE" >&2; exit 1; } ;;
esac

[ "$NOH" = 1 ] && { [ "$LLM" = 1 ] || [ "$MOD" = 1 ]; } && { echo "--no-heuristics cannot be combined with --llm / --modernization" >&2; exit 1; }
[ "$LLM" = 1 ] && [ "$MOD" = 1 ] && { echo "--llm and --modernization are mutually exclusive" >&2; exit 1; }
LLM_ARGS=()
[ "$NOH" = 1 ] && LLM_ARGS=(--rules none)
if [ "$MOD" = 1 ]; then
  command -v claude >/dev/null || { echo "--modernization needs the 'claude' CLI in PATH" >&2; exit 1; }
  claude plugin list 2>/dev/null | grep -q code-modernization || {
    echo "Plugin not installed. Run once:  claude plugin install code-modernization@claude-plugins-official" >&2; exit 1; }
  if [ "$YES" != 1 ]; then
    echo "The code-modernization plugin starts MANY Claude agents (~10-15 per module, ~16k tokens each; minutes to hours)"
    echo "and sends the source code of:  $SOURCE  to Anthropic. Business rules will come ONLY from the plugin."
    [ -z "$MODULES" ] && echo "Tip: start with one module: --modules <pattern>"
    read -r -p "Proceed? [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || { echo "aborted"; exit 1; }
  fi
  LLM_ARGS=(--rules modernization --yes)
  [ -n "$MODULES" ] && LLM_ARGS+=(--modernization-pattern "$MODULES")
fi
if [ "$LLM" = 1 ]; then
  if ! command -v claude >/dev/null && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "--llm needs the 'claude' CLI in PATH or ANTHROPIC_API_KEY" >&2; exit 1
  fi
  if [ "$YES" != 1 ]; then
    echo "Claude enrichment will send the source code of services, controllers and entities of:"
    echo "  $SOURCE"
    echo "to Anthropic. Continue only if your client/project data policy allows it."
    read -r -p "Proceed? [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || { echo "aborted"; exit 1; }
  fi
  LLM_ARGS=(--llm)
fi

if [ "$TEST" = 1 ]; then
  echo "Running tests..."
  python3 -m unittest discover -s tests 2>&1 | tail -3
fi

URL="http://127.0.0.1:${PORT}"
echo "Starting on ${URL}  (scanning: ${SOURCE}${LLM_ARGS:+, extra rule options: ${LLM_ARGS[*]}}) - Ctrl+C to stop"
# open the browser once the server answers (macOS: open, Linux: xdg-open)
( for _ in $(seq 1 30); do
    curl -fs "${URL}/api/meta" >/dev/null 2>&1 && { (open "$URL" || xdg-open "$URL") >/dev/null 2>&1; break; }
    sleep 0.3
  done ) &

exec python3 -m kgraph serve --port "$PORT" --scan "$SOURCE" ${LLM_ARGS[@]+"${LLM_ARGS[@]}"}
