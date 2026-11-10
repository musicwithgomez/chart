@echo off
REM 1) Runs the automatic self-test (whole customer journey with a fake MT5, about 30 seconds)
REM 2) Then starts the website on this PC in TEST MODE and opens it, so you can click through it yourself.
cd /d "%~dp0.."
if not exist ".venv" py -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r saas\requirements.txt httpx
echo.
python saas\selftest\selftest.py
echo.
if "%GP_ADMIN_TOKEN%"=="" set GP_ADMIN_TOKEN=admin-local-test
echo Starting the website in TEST MODE: http://127.0.0.1:8800
echo  - Use the "Test tools" panel (bottom left) to create customers and jump between trial / paid / expired.
echo  - Admin tab token: %GP_ADMIN_TOKEN%
echo Keep this window open. Close it to stop.
start "" http://127.0.0.1:8800
python -m uvicorn saas.license_server:app --host 127.0.0.1 --port 8800
pause
