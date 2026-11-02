@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install it from python.org and tick "Add python.exe to PATH". & pause & exit /b 1)
python -m pip install -q -r requirements.txt
if not exist .env copy .env.example .env >nul
python seed.py --if-empty
echo.
echo   Open http://localhost:8000   (admin / CareFi@2026)
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*
pause
