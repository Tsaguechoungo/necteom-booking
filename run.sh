#!/bin/sh
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
[ -n "$PY" ] || { echo "Python not found. Install Python 3.10+ from https://www.python.org/downloads/"; exit 1; }
[ -x venv/bin/python ] || "$PY" -m venv venv || { echo "Could not create the virtual environment."; exit 1; }
venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt || { echo "Install failed: check your internet connection and run again."; exit 1; }
venv/bin/python seed_demo.py
venv/bin/python app.py
