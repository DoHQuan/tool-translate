@echo off
cd /d "%~dp0.chat2api"
echo Chat2api: http://127.0.0.1:5005
echo Giu cua so nay mo khi dung tool. Nhan Ctrl+C de dung server.
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 5005
pause
