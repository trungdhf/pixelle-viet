@echo off
cd /d %~dp0
where ffmpeg >nul 2>nul || (echo Chua cai ffmpeg. Chay: winget install Gyan.FFmpeg & pause & exit /b 1)
if not exist service-account.json if "%GCP_SA_KEY_JSON%"=="" (echo Thieu file service-account.json & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
  python -m venv .venv || (echo Chua cai Python 3.12 & pause & exit /b 1)
  .venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
  .venv\Scripts\python -m playwright install chromium
)
start "" http://localhost:8080/series
.venv\Scripts\python -m uvicorn app.server:app --port 8080
pause
