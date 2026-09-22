@echo off
chcp 65001 >nul 2>nul
title Hanhua Tool
cd /d "%~dp0"
rem Prefer the packaged exe if it exists (wildcard avoids encoding trouble)
for %%f in (dist\*.exe) do (
  start "" "%%f"
  exit /b
)
set "PY=C:\Users\keymi\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0hanhua.py" %*
if "%~1"=="" pause
