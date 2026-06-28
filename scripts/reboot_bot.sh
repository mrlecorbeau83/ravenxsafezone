#!/bin/bash
# Si service systemd configuré, utiliser ça :
# systemctl --user restart savezone-bot

# Sinon kill + le script lancer_bot.sh gère le redémarrage auto
pkill -f "ravenXsavezone/main.py" 2>/dev/null
echo "🔄 SaveZone redémarré (le script lancer_bot.sh relance auto)"
