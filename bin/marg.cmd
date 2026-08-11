@echo off
rem marginalia CLI entrypoint for Windows shells.
rem Probes for a working Python 3.11+; `python` on PATH is often the Microsoft
rem Store alias stub, which is not an interpreter.
setlocal
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"

set "MARG_PY="
call :try "%MARGINALIA_PYTHON%"
if not defined MARG_PY call :try "%ROOT%\.venv\Scripts\python.exe"
if not defined MARG_PY call :try "python"

if defined MARG_PY goto run
echo marg: no working Python 3.11+ found. 1>&2
echo marg: tried MARGINALIA_PYTHON, %ROOT%\.venv, python 1>&2
echo marg: set MARGINALIA_PYTHON to a Python 3.11+ interpreter. 1>&2
endlocal & exit /b 127

:run
"%MARG_PY%" -m marginalia.cli %*
endlocal & exit /b %ERRORLEVEL%

:try
if "%~1"=="" exit /b 0
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "MARG_PY=%~1"
exit /b 0
