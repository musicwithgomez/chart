@echo off
REM Try Goldmine Pro exactly like a new customer, while YOUR own bridge keeps running.
REM 1) This starts the website (test mode) on http://127.0.0.1:8800 and opens it.
REM 2) Sign up, download the zip, unzip it anywhere, double-click START GOLDMINE.
REM    That customer bridge starts on port 8766 (your own bridge keeps 8765) and the website finds it by itself.
cd /d "%~dp0.."
if not exist ".venv" py -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r saas\requirements.txt
if "%GP_ADMIN_TOKEN%"=="" set GP_ADMIN_TOKEN=admin-local-test
echo.
echo  Website: http://127.0.0.1:8800   (keep this window open while you test)
echo  Next: Start free trial  ^>  Download  ^>  unzip  ^>  double-click START GOLDMINE
echo.
start "" http://127.0.0.1:8800
python -m uvicorn saas.license_server:app --host 127.0.0.1 --port 8800
pause
