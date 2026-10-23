@echo off
setlocal

REM ===== Settings =====
set "PYTHON_WINGET_ID=Python.Python.3.14"
set "REQUIRED_PKGS=google-genai Pillow certifi"
set "OPEN_PYTHON_DOWNLOADS=1"
REM ====================

echo [1/9] Checking winget, refreshing community source...
where winget >nul 2>&1
if errorlevel 1 (
  echo Winget is not available, please install Python from python.org, then run this installer again.
  if "%OPEN_PYTHON_DOWNLOADS%"=="1" start "" https://www.python.org/downloads/windows/
  goto :HOLD_OPEN_FAIL
)
REM Refresh only the community repo; do not depend on msstore
winget source reset winget --force >nul 2>&1
winget source update winget >nul 2>&1

echo [2/9] Searching for a real python.exe, skipping WindowsApps stub...
set "PYEXE="

REM Try PATH, but skip WindowsApps stub
for /f "usebackq delims=" %%P in (`where python 2^>nul`) do (
  echo %%P | find /I "\WindowsApps\python.exe" >nul
  if errorlevel 1 (
    set "PYEXE=%%P"
    goto :HAVE_PY
  )
)

REM Try per-user installs
for /f "delims=" %%D in ('dir /b /ad "%LocalAppData%\Programs\Python\Python3*" 2^>nul') do (
  if exist "%LocalAppData%\Programs\Python\%%D\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\%%D\python.exe"
)
if defined PYEXE goto :HAVE_PY

REM Try Program Files
for /f "delims=" %%D in ('dir /b /ad "C:\Program Files\Python3*" 2^>nul') do (
  if exist "C:\Program Files\%%D\python.exe" set "PYEXE=C:\Program Files\%%D\python.exe"
)
if defined PYEXE goto :HAVE_PY

echo [3/9] Python not found, installing "%PYTHON_WINGET_ID%" from the winget source...
winget install -e --id "%PYTHON_WINGET_ID%" --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo Could not install Python via winget, please install from python.org, then run this installer again.
  if "%OPEN_PYTHON_DOWNLOADS%"=="1" start "" https://www.python.org/downloads/windows/
  goto :HOLD_OPEN_FAIL
)

REM Re-scan after install
for /f "delims=" %%D in ('dir /b /ad "%LocalAppData%\Programs\Python\Python3*" 2^>nul') do (
  if exist "%LocalAppData%\Programs\Python\%%D\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\%%D\python.exe"
)
if not defined PYEXE (
  echo Python was installed, but no python.exe was found, please reboot or install manually.
  goto :HOLD_OPEN_FAIL
)

:HAVE_PY
echo [4/9] Using: %PYEXE%
"%PYEXE%" -c "print('ping')" >nul 2>&1 || (
  echo Found python, but it does not run, check PATH or reinstall Python.
  goto :HOLD_OPEN_FAIL
)

REM Hint: if WindowsApps Store stub still appears in PATH, suggest disabling aliases
for /f "usebackq delims=" %%P in (`where python 2^>nul`) do (
  echo %%P | find /I "\WindowsApps\python.exe" >nul
  if not errorlevel 1 (
    echo Notice, Windows App Execution Aliases for Python seem enabled, this is a Microsoft Store stub.
    echo Open Settings, Apps, App execution aliases, disable Python toggles, then re-run if needed.
  )
)

echo [5/9] Ensuring pip using ensurepip...
"%PYEXE%" -m ensurepip --upgrade 1>nul 2>nul

echo [6/9] Upgrading pip for current user...
"%PYEXE%" -m pip install --user -U pip 1>nul 2>nul

echo [7/9] Installing packages for current user: %REQUIRED_PKGS% ...
"%PYEXE%" -m pip install --user %REQUIRED_PKGS%
if errorlevel 1 (
  echo Package installation failed, check Internet or proxy settings, then run this again.
  goto :HOLD_OPEN_FAIL
)

echo [8/9] Verifying imports...
"%PYEXE%" -c "import importlib; [importlib.import_module(m) for m in ('google.genai','PIL','certifi')]; print('OK')" >nul 2>&1
if errorlevel 1 (
  echo Import check failed, make sure the same python.exe is used.
  goto :HOLD_OPEN_FAIL
)

set /p ANS=Store GEMINI_API_KEY as a user environment variable now? (Y/N): 
if /I "%ANS%"=="Y" (
  set /p GEM=Enter GEMINI_API_KEY value: 
  if not "%GEM%"=="" (
    setx GEMINI_API_KEY "%GEM%" >nul
    echo GEMINI_API_KEY saved for the current user.
  )
)

echo.
echo [9/9] env installed successfully, you can close this window
echo (the window will stay open, close it manually)
goto :HOLD_OPEN_OK

:HOLD_OPEN_FAIL
echo.
echo Setup finished with warnings or errors, please review the messages above.
echo (the window will stay open, close it manually)

:HOLD_OPEN_OK
echo.
echo Press any key to close...
pause >nul
timeout /t 86400 /nobreak >nul
exit /b 0
