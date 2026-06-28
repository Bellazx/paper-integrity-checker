#!/bin/bash
# Run paper integrity checker using the project venv
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python"
exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
