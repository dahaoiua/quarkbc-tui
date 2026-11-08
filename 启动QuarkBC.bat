@echo off
cd /d %~dp0
python quarkbc.py %*
if errorlevel 1 pause
