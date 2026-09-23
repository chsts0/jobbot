@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY ( echo Сначала запусти install.bat & pause & exit /b )
echo JobBot запущен. Пока это окно открыто - бот работает. Закрыть окно = остановить бота.
%PY% main.py
pause
