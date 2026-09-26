@echo off
setlocal

rem Actual project directory: %USERPROFILE%\Desktop\_dst
set "PROJECT_DIR=%USERPROFILE%\Desktop\_dst"

cd /d "%PROJECT_DIR%" || (
    echo Could not open project folder: %PROJECT_DIR%
    pause
    exit /b 1
)

echo Installing dependencies...
python -m pip install -r "%PROJECT_DIR%\requirements.txt"
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

echo Starting DST...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\run_dev.ps1"

pause
endlocal
