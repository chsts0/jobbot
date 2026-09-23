@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo Python не найден. Ставлю Python 3.12 через winget...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo Python установлен. Закрой это окно и запусти install.bat ещё раз.
  pause
  exit /b
)
echo Ставлю библиотеки...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Что-то не установилось - пришли Claude скриншот этого окна.
  pause
  exit /b
)
if not exist .env copy .env.example .env >nul
echo.
echo ============================================================
echo  Готово. Сейчас откроется файл .env в Блокноте.
echo  Впиши туда TG_API_ID, TG_API_HASH и BOT_TOKEN - где взять, написано в README.
echo  Сохрани (Ctrl+S) и запусти login.bat
echo ============================================================
start notepad .env
pause
