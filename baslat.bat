@echo off
setlocal
title WhatsAppTransporter
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "CHROMIUM_MARKER=%~dp0.venv\.playwright-chromium-installed"

if not exist "%VENV_PY%" (
    echo Proje icin Python sanal ortami hazirlaniyor...
    py -3 -m venv "%~dp0.venv"
    if errorlevel 1 python -m venv "%~dp0.venv"
    if errorlevel 1 goto setup_error
)

"%VENV_PY%" -c "import flask, playwright, waitress" >nul 2>nul
if errorlevel 1 (
    echo Gerekli Python paketleri kuruluyor. Internet baglantisi gerekebilir...
    "%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 goto setup_error
)

if not exist "%CHROMIUM_MARKER%" (
    echo Playwright Chromium tarayicisi kuruluyor...
    "%VENV_PY%" -m playwright install chromium
    if errorlevel 1 goto setup_error
    type nul > "%CHROMIUM_MARKER%"
)

"%VENV_PY%" wa_panel.py
if errorlevel 1 echo Uygulama hata vererek kapandi.
goto end

:setup_error
echo Kurulum tamamlanamadi. Python 3 ve internet baglantisini kontrol edin.
echo Ayrinti icin bu penceredeki hata mesajlarini inceleyin.

:end
pause
endlocal
