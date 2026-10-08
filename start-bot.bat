@echo off
rem Runs the bot forever: restarts it 10s after any exit (crash, network
rem not up yet at boot, etc). Output goes to bot.log next to this file.
cd /d "%~dp0"
:loop
.venv\Scripts\python.exe -m bot >> bot.log 2>&1
rem ping as a sleep: timeout.exe needs a console, which a boot task lacks
ping -n 11 127.0.0.1 >nul
goto loop
