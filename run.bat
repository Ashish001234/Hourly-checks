@echo off
REM Run the dashboard locally: refresh the listings, then serve and open it.
REM
REM   run.bat        quick refresh (~20s) - polls the boards that actually
REM                  produce new-grad roles, plus all four trackers
REM   run.bat full   full sweep (~2 min) - every known company board, and
REM                  recomputes which ones are worth polling quickly
REM
REM A local server is required: the page fetches data/jobs.json, and browsers
REM block that over file://. Leave this window open while you use the board;
REM close it or press Ctrl+C when you are done.

cd /d "%~dp0"

set MODE=
if /i "%~1"=="full" set MODE=--full

echo.
echo === Refreshing listings %MODE% ===
py -3 scripts\fetch_jobs.py %MODE%
if errorlevel 1 (
  echo.
  echo Refresh failed - serving the last good data instead.
)

echo.
echo === Dashboard: http://127.0.0.1:8765/ ===
echo Press Ctrl+C to stop.
echo.
start "" http://127.0.0.1:8765/
py -3 -m http.server 8765 --bind 127.0.0.1
