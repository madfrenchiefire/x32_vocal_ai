@echo off
REM Build X32SonicSniper.exe on Windows. See docs/PACKAGING.md for details
REM (ASIO DLL, license keypair, code-signing, selling keys).

setlocal

if not exist ".venv\Scripts\activate.bat" (
    echo No .venv found. Create one first:
    echo     py -3.12 -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt pyinstaller
    exit /b 1
)
call .venv\Scripts\activate.bat

python -c "from app.licensing.public_key import PUBLIC_KEY_HEX; import sys; sys.exit(0 if PUBLIC_KEY_HEX else 1)"
if errorlevel 1 (
    echo.
    echo WARNING: no signing keypair yet. Purchased keys won't validate until you run:
    echo     python -m app.tools.license_gen init
    echo The trial will still work. Continuing in 5 seconds... Ctrl+C to abort.
    timeout /t 5 >nul
)

echo Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Building...
pyinstaller x32sonicsniper.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Done: dist\X32SonicSniper.exe
echo Reminder: verify ASIO devices appear in Device Setup (see docs/PACKAGING.md).
endlocal
