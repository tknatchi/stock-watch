"""
dashboard.html + portfolio_dashboard.html を1つのArtifact(タブ切り替え)にまとめる

やっていること:
    combined_dashboard_template.html の中に、既存の2つの完成済みHTML
    (dashboard.html / portfolio_dashboard.html)をそれぞれbase64で丸ごと埋め込み、
    タブ切り替えで表示するiframeのsrcdocとして読み込ませる。
    2つのページは元々別々に作られた独立したJS/CSSを持つため、1つのDOMに
    直接マージすると変数名やCSSのグローバル定義が衝突しうる。iframeで
    ブラウジングコンテキストごと分離することで、それぞれ無改造のまま安全に
    同居させている。

前提:
    先に以下を実行して dashboard.html / portfolio_dashboard.html を
    最新化しておくこと:
        python export_dashboard_data.py && python generate_dashboard.py
        python export_portfolio_data.py && python generate_portfolio_dashboard.py

使い方:
    python generate_combined_dashboard.py
"""

from __future__ import annotations

import base64
from pathlib import Path

import console_utf8

console_utf8.setup()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "combined_dashboard_template.html"
STOCK_HTML_PATH = BASE_DIR / "dashboard.html"
PORTFOLIO_HTML_PATH = BASE_DIR / "portfolio_dashboard.html"
OUT_PATH = BASE_DIR / "combined_dashboard.html"


def b64(path: Path) -> str:
    return base64.b64encode(path.read_text(encoding="utf-8").encode("utf-8")).decode("ascii")


def main() -> None:
    missing = [p for p in (STOCK_HTML_PATH, PORTFOLIO_HTML_PATH) if not p.exists()]
    if missing:
        names = ", ".join(p.name for p in missing)
        raise SystemExit(
            f"{names} が見つかりません。先に generate_dashboard.py / "
            f"generate_portfolio_dashboard.py を実行してください。"
        )

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("__STOCK_HTML_B64__", b64(STOCK_HTML_PATH))
    html = html.replace("__PORTFOLIO_HTML_B64__", b64(PORTFOLIO_HTML_PATH))

    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[生成しました] {OUT_PATH}")


if __name__ == "__main__":
    main()
