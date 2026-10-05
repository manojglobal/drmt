@echo off
echo ========================================================
echo   DRMT V2 - Standalone Executable Build Script
echo ========================================================

echo.
echo [1/3] Installing and verifying dependencies...
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo.
echo [2/3] Building executable via PyInstaller...
python -m PyInstaller drmt.spec --clean --noconfirm

echo.
echo [3/3] Preparing deployment folders...
if not exist "dist\drmt\static" mkdir "dist\drmt\static"
if not exist "dist\drmt\data" mkdir "dist\drmt\data"
if not exist "dist\drmt\data\logs" mkdir "dist\drmt\data\logs"

if exist "data\drmt.db" (
    copy /Y "data\drmt.db" "dist\drmt\data\"
)
if exist ".env" (
    copy /Y ".env" "dist\drmt\.env"
) else if exist ".env.example" (
    copy /Y ".env.example" "dist\drmt\.env"
)

rem Create one-click launcher in dist\drmt
(
echo @echo off
echo title DRMT V2 Server
echo cd /d "%%~dp0"
echo echo Starting DRMT V2 Server on port 8000...
echo drmt.exe
echo pause
) > "dist\drmt\run.bat"

echo.
echo ========================================================
echo   BUILD SUCCESSFUL!
echo   Executable package: dist\drmt\
echo   Run using: dist\drmt\run.bat or dist\drmt\drmt.exe
echo ========================================================
