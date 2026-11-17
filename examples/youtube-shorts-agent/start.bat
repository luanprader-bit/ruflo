@echo off
rem Doppelklick startet den YouTube Shorts Agent.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
echo.
pause
