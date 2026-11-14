#!/bin/bash
# Swarm board CLI. Implementation and schema live in board.py beside this file.
exec python3 "$(dirname "$0")/board.py" "$@"
