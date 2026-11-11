@echo off
chcp 65001 >nul
cd /d "%~dp0"
:loop
echo.
set "LINK="
set /p LINK=Dan link Douyin (Enter de thoat):
if "%LINK%"=="" goto :eof
python douyin_dub.py "%LINK%"
echo.
explorer output
goto loop
