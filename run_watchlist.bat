@echo off
cd /d "%~dp0"
git pull --quiet >> logs\run_history.log 2>&1
python watchlist.py >> logs\run_history.log 2>&1
python export_dashboard_data.py >> logs\run_history.log 2>&1
python generate_dashboard.py >> logs\run_history.log 2>&1

rem 資産管理側のデータも、株モニタリングと同じダッシュボード(タブ統合)に載せるため
rem ここで一緒に更新する。portfolio.json(バイ&ホールド組の開始状態)が無い場合はスキップ。
if exist portfolio.json (
    python export_portfolio_data.py >> logs\run_history.log 2>&1
    python generate_portfolio_dashboard.py >> logs\run_history.log 2>&1
)

rem クラウド側の自動公開ジョブ(stock-watch dashboard daily republish)が
rem Yahoo Financeへ直接アクセスできないため、ここで生成した最新データを
rem dashboard_data_latest.json / portfolio_dashboard_data_latest.json としてリポジトリに
rem コミット・pushしておく。クラウド側はこの2つを取得してHTMLを組み立て、
rem 1つのArtifact(タブ切り替え)として再公開するだけでよい。
rem 以降のrebalance.py/rebalance_auto.pyは時間がかかったり中断されうるため、
rem 一番大事な公開データの更新を先に確定させる(途中で切れても当日分は反映される)。
copy /y logs\dashboard_data.json dashboard_data_latest.json >> logs\run_history.log 2>&1
if exist logs\portfolio_dashboard_data.json copy /y logs\portfolio_dashboard_data.json portfolio_dashboard_data_latest.json >> logs\run_history.log 2>&1
git add dashboard_data_latest.json portfolio_dashboard_data_latest.json >> logs\run_history.log 2>&1
git commit -m "dashboard_data_latest.json / portfolio_dashboard_data_latest.json自動更新" >> logs\run_history.log 2>&1
git push --quiet >> logs\run_history.log 2>&1

rem ローカルでも統合ダッシュボードを生成しておく(手動確認・手動publish用。cloud側は自分で組み立てる)
if exist dashboard.html if exist portfolio_dashboard.html python generate_combined_dashboard.py >> logs\run_history.log 2>&1

if exist portfolio.json python rebalance.py >> logs\run_history.log 2>&1
if exist portfolio_auto.json python rebalance_auto.py >> logs\run_history.log 2>&1
