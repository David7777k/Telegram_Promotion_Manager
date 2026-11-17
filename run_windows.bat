@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0TelegramPromotionStudio.exe" (
    start "" "%~dp0TelegramPromotionStudio.exe"
    exit /b 0
)
where pyw >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    start "" pyw -3 "%~dp0app.py"
    exit /b 0
)
where pythonw >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    start "" pythonw "%~dp0app.py"
    exit /b 0
)
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 "%~dp0app.py"
    if errorlevel 1 pause
    exit /b
)
echo Python 3.10+ not found. Install Python and enable Add Python to PATH.
pause
