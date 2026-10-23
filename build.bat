@echo off
rem Builds dist\AdaptiveBrightness.exe (a single file you can share).
rem Needs Python 3.10+ from python.org installed and on PATH.
setlocal
cd /d "%~dp0"

python -m venv .venv || goto :fail
call .venv\Scripts\activate.bat || goto :fail
python -m pip install --upgrade pip || goto :fail
pip install -r requirements.txt pyinstaller || goto :fail

if not exist icon.ico python -c "from icon_art import save_ico; save_ico('icon.ico')"

pyinstaller --noconfirm --clean --onefile --windowed ^
  --name AdaptiveBrightness --icon icon.ico ^
  --collect-all screen_brightness_control ^
  --hidden-import wmi --hidden-import pythoncom --hidden-import pywintypes --hidden-import win32com.client ^
  app.py || goto :fail

echo.
echo Done. Your program is: dist\AdaptiveBrightness.exe
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
exit /b 1
