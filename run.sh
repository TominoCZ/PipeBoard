#!/usr/bin/env bash
# Build and run PipeBoard. Arguments go to the `pipeboard` command:
#   ./run.sh               start and open the window (live PipeWire)
#   ./run.sh --fixture     the same UI on the saved graph; never touches PipeWire
#   ./run.sh --no-window   serve only (http://127.0.0.1:7870)
#   ./run.sh shots [name]  render the board in headless Chrome to .dev/shots/ and audit it
#   ./run.sh check         run the checks
#   ./run.sh perf [scene]  what drawing costs, headless Chrome on the GPU (scripts/perf.py)
set -euo pipefail
cd "$(dirname "$0")"
command -v uv >/dev/null || { echo "run.sh: uv is not installed: https://docs.astral.sh/uv/" >&2; exit 1; }
case "${1:-}" in
  shots) shift; exec uv run --with playwright python scripts/shots.py "$@" ;;
  check) shift; exec uv run python tests/check.py "$@" ;;
  perf) shift; exec uv run python scripts/perf.py "$@" ;;
esac
uv sync --quiet
exec uv run pipeboard "$@"
