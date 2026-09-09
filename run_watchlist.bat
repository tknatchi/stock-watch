@echo off
cd /d "%~dp0"
git pull --quiet >> logs\run_history.log 2>&1
python watchlist.py >> logs\run_history.log 2>&1
python export_dashboard_data.py >> logs\run_history.log 2>&1
python generate_dashboard.py >> logs\run_history.log 2>&1
if exist portfolio.json python rebalance.py >> logs\run_history.log 2>&1

rem クラウド側の自動公開ジョブ(stock-watch dashboard daily republish)が
rem Yahoo Financeへ直接アクセスできないため、ここで生成した最新データを
rem dashboard_data_latest.json としてリポジトリにコミット・pushしておく。
rem クラウド側はこのファイルを取得してHTMLを組み立て、Artifactを再公開するだけでよい。
copy /y logs\dashboard_data.json dashboard_data_latest.json >> logs\run_history.log 2>&1
git add dashboard_data_latest.json >> logs\run_history.log 2>&1
git commit -m "dashboard_data_latest.json自動更新" >> logs\run_history.log 2>&1
git push --quiet >> logs\run_history.log 2>&1
