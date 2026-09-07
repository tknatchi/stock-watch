@echo off
cd /d "%~dp0"
git pull --quiet >> logs\run_history.log 2>&1
python watchlist.py >> logs\run_history.log 2>&1
python export_dashboard_data.py >> logs\run_history.log 2>&1
python generate_dashboard.py >> logs\run_history.log 2>&1
