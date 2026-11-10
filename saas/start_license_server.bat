@echo off
REM Starts the Goldmine Pro license server on THIS PC in TEST MODE (no real payments).
REM Then open http://127.0.0.1:8800 to see the customer website, sign-up, dashboard, billing and admin.
cd /d "%~dp0.."
if not exist ".venv" py -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r saas\requirements.txt
if "%GP_ADMIN_TOKEN%"=="" set GP_ADMIN_TOKEN=admin-local-test
echo.
echo  Customer site : http://127.0.0.1:8800
echo  Admin tab     : log in, open http://127.0.0.1:8800/#admin  (token: %GP_ADMIN_TOKEN%)
echo  Keep this window open.
echo.
python -m uvicorn saas.license_server:app --host 127.0.0.1 --port 8800
pause
