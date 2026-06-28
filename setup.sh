#!/bin/bash
set -e

echo "==> Création du virtualenv..."
python3 -m venv .venv

echo "==> Installation des dépendances..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt

echo ""
echo "✓ Prêt. Lance le bot avec :"
echo "  .venv/bin/python main.py"
