#!/bin/sh
# Grayline launcher — runs the server with the venv's interpreter (which has
# paho-mqtt), so you never hit "No module named paho." No need to 'activate'
# anything; calling venv/bin/python directly uses the venv. Passes through any
# extra args to the server.
cd "$(dirname "$0")"
if [ ! -x venv/bin/python ]; then
    echo "No venv yet — run ./install.sh first."
    exit 1
fi
exec ./venv/bin/python grayline_server.py "$@"
