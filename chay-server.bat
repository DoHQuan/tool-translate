@echo off
title Long Tieng Viet - server
cd /d "%~dp0"
echo Dang mo server tai http://localhost:5577 ...
echo De tat server: dong cua so nay.
start "" /b cmd /c "timeout /t 3 >nul & start http://localhost:5577"
python server.py
echo.
echo Server da dung. Neu bao loi cong 5577 dang duoc dung thi server da chay san roi.
pause
