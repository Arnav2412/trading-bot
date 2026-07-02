@echo off
cd /d "%~dp0.."
set "PY=python"
if exist "%~dp0python_path.txt" set /p PY=<"%~dp0python_path.txt"
"%PY%" run.py premarket >> "reports\scheduler.log" 2>&1
