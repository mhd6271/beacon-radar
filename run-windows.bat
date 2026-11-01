@echo off
cd /d "%~dp0"
echo Beacon-Radar startet ...
start "Beacon-Radar" cmd /k python beacon_radar.py
timeout /t 3 /nobreak >nul
start "" http://localhost:8787
