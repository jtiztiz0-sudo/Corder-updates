@echo off
rem Double-click this to open the app.
rem
rem runtime\ is this app's own private Python -- it is used first and on its
rem own, so the app cannot be broken by whatever Python is or isn't installed
rem on this computer, or by one being installed/removed later.
rem
rem If there is no runtime\ folder (a delivery that was not bundled), fall
rem back to a system Python. pythonw = no black console window behind it.
cd /d "%~dp0"

rem --- refuse to run from inside a zip -------------------------------------
rem Double-clicking a .zip only LOOKS like opening a folder: Windows unpacks
rem it under %TEMP% and clears that out later. The app would run, she would
rem type a week of records into it, and they would disappear with the temp
rem folder. Cheaper to stop here than to explain that afterwards.
set "APPDIR=%~dp0"
call set "OUTSIDE=%%APPDIR:%TEMP%=%%"
if not "%OUTSIDE%"=="%APPDIR%" (
  echo.
  echo   This is still inside the zip file.
  echo.
  echo   Right-click the zip and choose "Extract All..." first, then put
  echo   the folder somewhere it can stay - C:\ is a good place.
  echo.
  echo   Anything you typed in from here would be deleted by Windows,
  echo   so the app will not start until it has been unzipped properly.
  echo.
  pause
  exit /b
)

if exist "%~dp0runtime\pythonw.exe" (
  start "" "%~dp0runtime\pythonw.exe" desktop.py
  goto :eof
)
if exist "%~dp0runtime\python.exe" (
  start "" "%~dp0runtime\python.exe" desktop.py
  goto :eof
)

where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw.exe desktop.py
  goto :eof
)
where python.exe >nul 2>nul
if %errorlevel%==0 (
  start "" python.exe desktop.py
  goto :eof
)

echo.
echo This app could not start because Python is not on this computer
echo and this copy was delivered without its own runtime folder.
echo.
echo Contact JTS and ask for the bundled version.
echo.
pause
