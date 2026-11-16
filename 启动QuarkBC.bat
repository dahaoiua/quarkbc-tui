@echo off
cd /d %~dp0
rem Prefer the project-local .venv so the Windows Store python stub never wins.
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" quarkbc.py %*
) else (
    python quarkbc.py %*
)
if errorlevel 1 pause
