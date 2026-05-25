#!/bin/bash
# Run paper integrity checker using the knowledge-base venv
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/opt/knowledge-base/venv/bin/python3"
exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
