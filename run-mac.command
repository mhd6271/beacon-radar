#!/bin/bash
cd "$(dirname "$0")"
clear
echo "BLE-Finder startet … (Fenster offen lassen, Strg+C beendet)"
[ -f venv/bin/activate ] && source venv/bin/activate
( sleep 2; open "http://localhost:8787" ) &
python3 beacon_radar.py
