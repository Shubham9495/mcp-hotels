@echo off
rem Runs make.ps1 without changing the system execution policy:  .\make up   .\make tunnel
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make.ps1" %*
