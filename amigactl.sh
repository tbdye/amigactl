#!/bin/sh
PYTHONPATH="$(dirname "$0")/client" exec python3 -m amigactl "$@"
