@echo off
rem Doppelklick: richtet alles ein und oeffnet die App (beim ersten Mal dauert es ein paar Minuten).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
if not "%~1"=="" pause
