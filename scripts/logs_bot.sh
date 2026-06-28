#!/bin/bash
DIR="/home/chrow/Bureau/ravenXsavezone"
gnome-terminal --title="🤖 SaveZone — Logs" -- bash -c "
  cd '$DIR'
  source venv/bin/activate
  python3 Scripts_SaveZone/bot_viewer.py
  exec bash
"
