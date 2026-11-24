@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set "MOSS_LAUNCHER=%~dp0chat.ps1"
if not exist "%MOSS_LAUNCHER%" set "MOSS_LAUNCHER=F:\moss\agent-lab\chat.ps1"
powershell.exe -NoProfile -File "%MOSS_LAUNCHER%" -Open
if errorlevel 1 pause
