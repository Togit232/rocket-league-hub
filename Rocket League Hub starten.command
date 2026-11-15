#!/bin/zsh
# Doppelklick startet den Rocket League Hub und oeffnet ihn im Browser.
# Dieses Fenster offen lassen, solange die App laufen soll (Beenden: Ctrl+C oder Fenster schliessen).
cd "$(dirname "$0")"
exec /usr/bin/python3 server.py --open
