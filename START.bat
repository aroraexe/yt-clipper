@echo off
title Shorts Downloader
color 0A
echo.
echo  ============================================
echo    Shorts Downloader - Starting...
echo  ============================================
echo.
echo  Opening http://localhost:5000 in your browser
echo  Press Ctrl+C to stop the server
echo.
start "" "http://localhost:5000"
python "%~dp0app.py"
pause
