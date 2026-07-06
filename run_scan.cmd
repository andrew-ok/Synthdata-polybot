@echo off
rem Polybot 15-min paper-trade scan (Strategy E). Scheduled via Task Scheduler.
cd /d "C:\Users\OKA\Cursor Folder\Synthdata-polybot"
set PYTHONIOENCODING=utf-8
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m polybot.scanner --execute --kelly >> polybot\logs\scan_loop.log 2>&1
rem scanner exits 1 when no trades accepted - that is a normal scan, not a failure
exit /b 0
