@echo off
setlocal enabledelayedexpansion
REM ==========================================================================
REM  ONE-TIME setup. Just double-click this file. Nothing to type.
REM  Creates 3 automatic jobs (Mon-Fri):
REM    09:40  Pre-market LOCK + email (after the open settles, live entries)
REM    10:00/12:00/14:00  Momentum watchlist updates (every 2 hours)
REM    16:00  End-of-day P&L + auto-tune review
REM ==========================================================================
set "DIR=%~dp0"

REM --- find the exact Python so the scheduler can always run it ---
set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
  echo.
  echo Could not find Python automatically. Open PowerShell, type:  where python
  echo and tell Claude what it prints. Setup stopped.
  echo.
  pause
  exit /b 1
)
> "%DIR%python_path.txt" echo %PY%
echo Using Python: %PY%
echo.

schtasks /create /tn "TradingBot Pre-Market" ^
  /tr "\"%DIR%run_premarket.bat\"" ^
  /sc weekly /d MON,TUE,WED,THU,FRI /st 09:40 /f

schtasks /create /tn "TradingBot Momentum Email" ^
  /tr "\"%DIR%run_email.bat\"" ^
  /sc weekly /d MON,TUE,WED,THU,FRI /st 10:00 /ri 120 /du 0530 /f

schtasks /create /tn "TradingBot EOD PnL" ^
  /tr "\"%DIR%run_eod.bat\"" ^
  /sc weekly /d MON,TUE,WED,THU,FRI /st 16:00 /f

echo.
echo ============================================================
echo  ALL SET. Emails will now arrive on their own, Mon-Fri:
echo    09:40  Pre-market LOCK (after the open settles)
echo    10:00 / 12:00 / 14:00  Momentum watchlist
echo    16:00  End-of-day P&L + review
echo  Keep your laptop ON and signed in during market hours.
echo  (To stop: double-click uninstall_schedule.bat)
echo ============================================================
echo.
pause
