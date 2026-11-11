@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Long Tieng Viet - dang chay (dong cua so nay de tat)
echo Dang mo web Long Tieng Viet tai http://localhost:5577
echo Dong cua so nay de tat web.
python server.py --open
pause
