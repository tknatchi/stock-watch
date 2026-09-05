@echo off
cd /d "%~dp0"
python watchlist.py >> logs\run_history.log 2>&1
