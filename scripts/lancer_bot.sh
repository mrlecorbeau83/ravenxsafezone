#!/bin/bash
DIR="/home/chrow/Bureau/ravenXsavezone"
gnome-terminal --title="🤖 SaveZone Bot" -- bash -c "
  cd '$DIR'
  source .venv/bin/activate
  echo '🤖 SaveZone lancé — relance auto en cas de crash'
  while true; do
    python3 /home/chrow/Bureau/ravenXsavezone/main.py
    echo '⚠️  Bot arrêté — relance dans 5 secondes... (Ctrl+C pour stopper)'
    sleep 5
  done
"
