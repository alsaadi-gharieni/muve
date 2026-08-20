@echo off
cd /d "%~dp0"
echo Using:
python -c "import sys; print(sys.executable)"
echo.
echo Installing WinRT packages...
python -m pip install --force-reinstall --disable-pip-version-check ^
  winrt-runtime ^
  winrt-Windows.Foundation ^
  winrt-Windows.Foundation.Collections ^
  winrt-Windows.Media.Audio ^
  winrt-Windows.Media.Control ^
  winrt-Windows.Devices.Enumeration ^
  winrt-Windows.Devices.Bluetooth ^
  winrt-Windows.Devices.Radios ^
  winrt-Windows.System
echo.
python -c "import winrt.windows.foundation.collections as c; print('SUCCESS:', c)"
if errorlevel 1 (
  echo FAILED — open this folder in cmd and check the errors above.
  pause
  exit /b 1
)
echo.
echo Done. Now run: python main.py
pause
