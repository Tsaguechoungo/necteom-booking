@echo off
cd /d "%~dp0"
echo.
echo === Necteom Room Booking - local start ===
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY goto nopy
if exist "venv\Scripts\python.exe" goto havevenv
echo Creating the virtual environment, please wait...
%PY% -m venv venv
if errorlevel 1 goto fail
:havevenv
echo Installing Flask, internet connection needed the first time...
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail
"venv\Scripts\python.exe" seed_demo.py
"venv\Scripts\python.exe" app.py
pause
exit /b 0
:nopy
echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/
echo and tick the box Add python.exe to PATH during installation. Then run this file again.
pause
exit /b 1
:fail
echo.
echo Something failed. Check your internet connection, then double-click run.bat again.
echo If it still fails, delete the folder named venv and try once more.
pause
exit /b 1
