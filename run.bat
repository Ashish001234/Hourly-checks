@echo off
REM Run the dashboard locally, or tailor resumes for your shortlist.
REM
REM   run.bat           quick refresh (~30s) then serve and open the board
REM   run.bat full      full sweep of every known company board (~2 min)
REM   run.bat tailor    build a resume for each starred job in shortlist.json
REM   run.bat resumes   rebuild the two base resumes
REM
REM For "tailor" you need:
REM   1. ANTHROPIC_API_KEY set in your environment
REM   2. shortlist.json in this folder -- star listings on the board, then hit
REM      "Export shortlist.json" on the Resumes tab and move the download here

cd /d "%~dp0"

if /i "%~1"=="tailor" goto tailor
if /i "%~1"=="resumes" goto resumes

set MODE=
if /i "%~1"=="full" set MODE=--full

echo.
echo === Refreshing listings %MODE% ===
py -3 scripts\fetch_jobs.py %MODE%
if errorlevel 1 (
  echo.
  echo Refresh failed - serving the last good data instead.
)
goto serve

:resumes
echo.
echo === Rebuilding base resumes ===
py -3 resumes\build.py --all
goto serve

:tailor
if not exist shortlist.json (
  echo.
  echo shortlist.json not found.
  echo Star some listings on the board, click "Export shortlist.json" on the
  echo Resumes tab, and move the downloaded file into this folder.
  echo.
  pause
  exit /b 1
)
if "%ANTHROPIC_API_KEY%"=="" (
  echo.
  echo ANTHROPIC_API_KEY is not set. Set it first, for example:
  echo     setx ANTHROPIC_API_KEY "sk-ant-..."
  echo then open a NEW terminal and run this again.
  echo.
  pause
  exit /b 1
)
echo.
echo === Tailoring resumes for your shortlist ===
py -3 resumes\tailor.py --shortlist shortlist.json
goto serve

:serve
echo.
echo === Dashboard: http://127.0.0.1:8765/ ===
echo Press Ctrl+C to stop.
echo.
start "" http://127.0.0.1:8765/
py -3 -m http.server 8765 --bind 127.0.0.1
