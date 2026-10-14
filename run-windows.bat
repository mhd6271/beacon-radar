@echo off
cd /d "%~dp0"
echo BLE-Finder startet ...
start "BLE-Finder" cmd /k python beacon_radar.py
timeout /t 3 /nobreak >nul
start "" http://localhost:8787
