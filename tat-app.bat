@echo off
title Tat app Long Tieng Viet
set FOUND=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":5577 .*LISTENING"') do (
  set FOUND=1
  taskkill /F /T /PID %%p >nul 2>&1
)
if defined FOUND (echo Da tat app Long Tieng Viet.) else (echo App dang khong chay.)
timeout /t 3 >nul
