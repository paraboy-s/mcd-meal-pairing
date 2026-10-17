@echo off
rem ============================================================
rem  McPair launcher - picks a Python >= 3.9 and runs the Skill entry.
rem  Double-click, or:  run.bat --demo
rem                     run.bat --demo --mode squad
rem  With no arguments it runs the built-in demo (no MCP Token needed).
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY_CMD="

rem --- 1. interpreters already on PATH -----------------------
call :probe python
if not "!PY_CMD!"=="" goto run
call :probe py -3
if not "!PY_CMD!"=="" goto run
call :probe python3
if not "!PY_CMD!"=="" goto run

rem --- 2. common install locations ---------------------------
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
  "C:\Python311\python.exe"
) do (
  if exist %%P (
    call :probe "%%~P"
    if not "!PY_CMD!"=="" goto run
  )
)

rem --- 3. conda environments (base is often too old) ---------
for %%R in (
  "%USERPROFILE%\anaconda3"
  "%USERPROFILE%\miniconda3"
  "%LOCALAPPDATA%\Continuum\anaconda3"
  "C:\ProgramData\Anaconda3"
  "C:\ProgramData\Miniconda3"
  "D:\anaconda"
  "D:\Anaconda3"
  "D:\miniconda3"
) do (
  if exist "%%~R\envs" (
    for /d %%E in ("%%~R\envs\*") do (
      if exist "%%~E\python.exe" (
        call :probe "%%~E\python.exe"
        if not "!PY_CMD!"=="" goto run
      )
    )
  )
)

rem --- nothing suitable found -------------------------------
echo.
echo   [X] No Python 3.9+ interpreter found.
echo.
echo   This project has zero dependencies - you only need a newer Python.
echo   Pick one of these:
echo.
echo       conda create -n mcpair python=3.11 -y
echo       conda activate mcpair
echo       python -m src.cli --budget 40
echo.
echo   Or install from https://www.python.org/downloads/  (tick "Add to PATH")
echo.
echo   Note: an old Anaconda base (Python 3.6) is the usual cause.
echo.
pause
exit /b 1

:run
echo Using interpreter: !PY_CMD!
echo.
if "%~1"=="" (
  !PY_CMD! scripts\pair.py --demo
) else (
  !PY_CMD! scripts\pair.py %*
)
set "RC=!errorlevel!"
rem keep the window open when launched by double-click (no arguments)
if "%~1"=="" pause
exit /b !RC!

:probe
rem %1 (and optional %2) form the command to test; sets PY_CMD on success
%~1 %~2 -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
if "%~2"=="" (
  set "PY_CMD=%~1"
) else (
  set "PY_CMD=%~1 %~2"
)
exit /b 0
