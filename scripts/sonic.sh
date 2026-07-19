#!/usr/bin/env bash
# Safe wrappers for zsh (inline # comments are NOT ignored unless interactivecomments is on)
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .venv/bin/activate ]]; then
  source .venv/bin/activate
fi
case "${1:-serve}" in
  serve)  exec sonic serve "${@:2}" ;;
  doctor) exec sonic doctor "${@:2}" ;;
  demo)   exec sonic run --demo "${@:2}" ;;
  *)      exec sonic "$@" ;;
esac
