#!/usr/bin/env python3
"""
Viewer de logs pour SaveZone Bot
Lance : python3 Scripts_SaveZone/bot_viewer.py
Lit depuis journalctl (service savezone-bot) ou un fichier log si pas de systemd
"""
import subprocess
import sys
import os
import time

try:
    from rich.console import Console
    from rich.text import Text
    from rich.live import Live
    from rich.panel import Panel
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "savezone.log")

# Couleurs par type de log (si rich dispo)
COLOR_MAP = {
    "[SaveZone]":    "cyan",
    "[GlobalBan":    "red",
    "[CrossBot":     "magenta",
    "[BanSync":      "yellow",
    "[Logs":         "blue",
    "ERROR":         "bold red",
    "Traceback":     "red",
    "✅":             "green",
    "❌":             "red",
    "⚠️":             "yellow",
    "🔄":             "cyan",
}

# Tags à filtrer complètement (trop verbeux)
IGNORE = [
    "ffmpeg",
    "heartbeat",
    "opus",
    "PyNaCl",
]


def colorize(line: str) -> "Text | str":
    if not HAS_RICH:
        return line
    t = Text(line)
    for keyword, color in COLOR_MAP.items():
        if keyword in line:
            t.stylize(color)
            break
    return t


def should_ignore(line: str) -> bool:
    lower = line.lower()
    return any(tag.lower() in lower for tag in IGNORE)


def stream_journalctl():
    """Lit les logs depuis le service systemd savezone-bot"""
    cmd = ["journalctl", "-u", "savezone-bot", "-f", "-n", "100", "--no-pager"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return proc


def stream_file(path: str):
    """Tail d'un fichier log"""
    # aller à la fin du fichier
    with open(path, "a"):  # crée si manquant
        pass
    proc = subprocess.Popen(["tail", "-f", "-n", "100", path],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return proc


def main():
    if HAS_RICH:
        console = Console()
        console.print(Panel("🤖 [bold cyan]SaveZone Bot — Logs en temps réel[/bold cyan]",
                            subtitle="Ctrl+C pour quitter"))
    else:
        print("=== SaveZone Bot — Logs ===")
        print("(installe 'rich' pour la couleur : pip install rich)")

    # essayer systemd d'abord, sinon fichier log
    use_file = False
    proc = None
    try:
        test = subprocess.run(["systemctl", "--user", "is-active", "savezone-bot"],
                              capture_output=True, text=True, timeout=2)
        if test.stdout.strip() in ("active", "activating"):
            proc = stream_journalctl()
        else:
            use_file = True
    except Exception:
        use_file = True

    if use_file:
        if not os.path.exists(LOG_FILE):
            if HAS_RICH:
                console.print(f"[yellow]Aucun service systemd trouvé. Lecture de {LOG_FILE}[/yellow]")
            else:
                print(f"Lecture de {LOG_FILE}")
        proc = stream_file(LOG_FILE)

    if not proc:
        print("Impossible de démarrer le viewer.")
        sys.exit(1)

    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line or should_ignore(line):
                continue
            if HAS_RICH:
                console.print(colorize(line))
            else:
                print(line)
    except KeyboardInterrupt:
        proc.terminate()
        if HAS_RICH:
            console.print("\n[bold red]Viewer arrêté.[/bold red]")
        else:
            print("\nArrêté.")


if __name__ == "__main__":
    main()
