@echo off
REM ---------------------------------------------------------------------------
REM Double-click launcher for the local web UI (launcher.py).
REM
REM Boots http://127.0.0.1:8765 and auto-opens it in your default browser.
REM Status panel, hub dropdown, action buttons, live log. Stays running until
REM you close this window or hit Ctrl-C.
REM
REM For one-shot incremental refresh + auto-open viewer (no UI), use
REM update_and_view.bat instead.
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

title Fusion Version History Launcher
echo === Starting Fusion Version History launcher ===
echo (Close this window or press Ctrl-C to stop.)
echo.
python launcher.py
set EXITCODE=%ERRORLEVEL%

if %EXITCODE% neq 0 (
    echo.
    echo  ! Launcher exited with code %EXITCODE%. Press any key to close.
    pause >nul
)
endlocal
