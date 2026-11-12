@echo off
title Kiem thu tool long tieng
cd /d "%~dp0"
echo Dang chay bo kiem thu (khoang 1 phut)...
python -W ignore -m unittest discover -s tests
echo.
if errorlevel 1 (echo CO BAI KIEM THU HONG - xem chi tiet o tren.) else (echo TAT CA DEU DAT.)
pause
