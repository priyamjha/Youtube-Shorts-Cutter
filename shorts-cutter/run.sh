#!/bin/bash
# Start the Shorts Cutter web app and open it in your browser.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up..."; ./setup.sh || exit 1
fi
exec .venv/bin/python app.py "$@"
