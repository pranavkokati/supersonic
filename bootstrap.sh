#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
echo ""
echo "Supersonic installed."
echo ""
echo "  source .venv/bin/activate"
echo "  sonic serve"
echo "  sonic doctor"
echo "  sonic run --demo"
echo ""
echo "Or: ./scripts/sonic.sh serve"
