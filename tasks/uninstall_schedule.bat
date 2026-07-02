@echo off
schtasks /delete /tn "TradingBot Momentum Email" /f
schtasks /delete /tn "TradingBot EOD PnL" /f
echo Removed both scheduled tasks.
pause
