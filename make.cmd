@echo off
rem Windows task runner:  .\make up   .\make tunnel   (runs scripts\tasks.ps1 without changing the execution policy)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\tasks.ps1" %*
