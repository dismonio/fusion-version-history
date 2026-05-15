@echo off
REM ---------------------------------------------------------------------------
REM Double-click launcher for the Fusion version-history workflow.
REM   1. Incremental walk (skips unchanged items)
REM   2. Regenerate the HTML viewer from the SQLite DB
REM   3. Open the viewer in the default browser
REM
REM Run-from-anywhere: cd to this .bat's own directory so paths resolve
REM regardless of where the user invoked it from.
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

echo === Refreshing Fusion version history (incremental) ===
echo.
python fusion_version_history.py --since-last-run
if errorlevel 1 (
    echo.
    echo  ! Walker step failed. Press any key to close.
    pause >nul
    exit /b 1
)

echo.
echo === Regenerating viewer HTML ===
echo.
python view.py
if errorlevel 1 (
    echo.
    echo  ! Viewer regeneration failed. Press any key to close.
    pause >nul
    exit /b 1
)

echo.
echo === Opening viewer in default browser ===
start "" "%~dp0fusion_versions.html"

endlocal
