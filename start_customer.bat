@echo off
rem Goldmine bridge for customers (shipped in the zip as "START GOLDMINE.bat").
rem Plain and visible on purpose: no PowerShell, no hidden windows. Keep this window open while you trade.
setlocal
cd /d "%~dp0"
title Goldmine bridge

rem 1) Find a working Python (skips the Microsoft Store placeholder that only opens the Store)
set "PY="
for %%c in (py python "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe") do (
  if not defined PY (
    "%%~c" -c "import sys" >nul 2>nul && set "PY=%%~c"
  )
)
if not defined PY (
  echo Python is needed once. Installing it from Microsoft's official package source ^(about 1 minute^)...
  where winget >nul 2>nul && winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
  echo.
  echo Could not install Python automatically.
  echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then double-click START GOLDMINE again.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)

rem 2) Own Python environment in this folder (first start only), then the packages
if not exist ".venv\Scripts\python.exe" (
  echo Preparing Goldmine ^(first start only^)...
  "%PY%" -m venv .venv
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt

rem 3) Run
".venv\Scripts\python.exe" run_bridge.py
pause
