"""
dashboard_template.html + logs/dashboard_data.json → dashboard.html を生成する

使い方:
    python export_dashboard_data.py   (先にデータを更新)
    python generate_dashboard.py      (このスクリプト)

生成された dashboard.html を Claude の Artifact として公開/再公開すると、
Web上のダッシュボードが最新のデータに更新される。
"""

from __future__ import annotations

from pathlib import Path

import console_utf8

console_utf8.setup()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "dashboard_template.html"
DATA_PATH = BASE_DIR / "logs" / "dashboard_data.json"
OUT_PATH = BASE_DIR / "dashboard.html"


def main() -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = DATA_PATH.read_text(encoding="utf-8")

    html = template.replace("__DASHBOARD_DATA_JSON__", data_json)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[生成しました] {OUT_PATH}")


if __name__ == "__main__":
    main()
