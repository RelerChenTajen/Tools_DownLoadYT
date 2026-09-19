@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
    set /p URL=請貼上 YouTube 網址: 
    python yt2mp3.py "%URL%"
) else (
    python yt2mp3.py %*
)
pause
