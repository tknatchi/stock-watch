"""
週次で「今、市場で話題のテーマ」をリサーチし、WATCHLIST未収録の関連銘柄候補を
theme_candidates.json に書き出すスクリプト。

やっていること:
    1. Googleニュース(日本語)から見出しを取得する。
       株式市場向けキーワード検索(日本株 材料 など)に加えて、
       ビジネス/テクノロジーの総合トピックフィードも混ぜることで、
       ステーブルコインのような「株式市場という言葉を含まない横断的な話題」も拾えるようにしている
    2. 現在のWATCHLIST(watchlist.py)と合わせてGeminiに渡し、
       「注目テーマ」とその関連候補企業(社名のみ)を抽出させる
    3. 結果を theme_candidates.json に保存する
       (export_dashboard_data.py が読み込み、ダッシュボードに反映する)

設計方針・注意点:
    - 証券コードはGeminiに出力させない(銘柄コードの取り違えリスクがあるため)。
      候補は社名のみを提示し、実際にWATCHLISTへ追加するかどうかは人間が確認してから行う。
    - このスクリプトはWATCHLISTを自動で書き換えない。あくまで「候補の提示」まで。
    - GitHub Actions (.github/workflows/theme-research.yml) から週次で実行される想定。
      ローカルでも `python theme_research.py` で単体実行できる。

必要な環境変数:
    GEMINI_API_KEY : Google AI StudioのGemini APIキー(news-line-botと共用)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

import console_utf8
from watchlist import JAPANESE_NAMES, WATCHLIST

console_utf8.setup()

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

NEWS_SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
NEWS_TOPIC_URL = "https://news.google.com/rss/headlines/section/topic/{topic}?hl=ja&gl=JP&ceid=JP:ja"

# キーワード検索: 株式市場に直結するニュースを狙い撃ちで拾う
SEARCH_QUERIES = ["日本株 材料", "東証 業種 上昇", "日本株 テーマ 物色"]
ARTICLES_PER_QUERY = 8

# トピック総合フィード: キーワードを事前に決め打ちしないぶん、
# ステーブルコインのような「株式市場の外から来る話題」も拾える
NEWS_TOPICS = ["BUSINESS", "TECHNOLOGY"]
ARTICLES_PER_TOPIC = 12

# news-line-bot/fetch_news.py と同じ理由で、モデルは明示的に固定する
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

BASE_DIR = Path(__file__).resolve().parent
OUT_PATH = BASE_DIR / "theme_candidates.json"


def _fetch_rss_titles(url: str, limit: int, seen: set[str]) -> list[str]:
    """1つのRSS URLから見出しを取得し、簡易的な重複除去をしたうえで返す。"""
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    titles = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        # Googleニュースの見出しは "本文 - 配信元" の形式なので配信元を切り落とす
        clean_title = re.sub(r"\s-\s[^-]+$", "", title).strip()

        key = clean_title[:15]
        if key in seen:
            continue
        seen.add(key)

        titles.append(clean_title)
        if len(titles) >= limit:
            break
    return titles


def fetch_market_news() -> list[str]:
    """株式市場向けキーワード検索 + 総合トピックフィードを両方取得してまとめる。

    キーワード検索だけだと「日本株」のような言葉を含む記事しか拾えず、
    ステーブルコインのような株式市場の外から来る横断的な話題を取りこぼす。
    トピック総合フィードを混ぜることで、事前にキーワードを決め打ちしなくても
    今動いている話題を拾えるようにしている。
    """
    seen: set[str] = set()
    articles: list[str] = []

    for query in SEARCH_QUERIES:
        url = NEWS_SEARCH_URL.format(query=requests.utils.quote(query))
        articles += _fetch_rss_titles(url, ARTICLES_PER_QUERY, seen)

    for topic in NEWS_TOPICS:
        url = NEWS_TOPIC_URL.format(topic=topic)
        articles += _fetch_rss_titles(url, ARTICLES_PER_TOPIC, seen)

    return articles


def build_prompt(headlines: list[str], watchlist_names: list[str]) -> str:
    headline_lines = "\n".join(f"- {h}" for h in headlines)
    watchlist_line = "、".join(watchlist_names)
    return f"""あなたは日本株の市場動向に詳しいアナリストです。
以下は直近の日本株・市場関連ニュースの見出し一覧です。

{headline_lines}

現在ウォッチ対象にしている銘柄(参考。これらは既知なので候補として繰り返す必要はない):
{watchlist_line}

これらの見出しから、今まさに市場で物色されている・注目度が上がっている「テーマ」を3〜5個抽出してください。
各テーマについて、そのテーマに関連する日本企業を2〜4社挙げてください。

重要な制約:
    - 証券コードは絶対に出力しないこと(社名のみ)。コードは人間が別途確認します。
    - 見出しから明確に裏付けられる、確度の高いテーマ・企業のみを挙げること。憶測で作らないこと。
    - 既にウォッチ対象の銘柄ばかりにならないよう、なるべく未収録の企業も含めること。

出力は次のJSON配列の形式のみ。前置き・説明文は一切不要です:
[
  {{
    "theme": "テーマ名(10〜20文字程度)",
    "summary": "そのテーマが注目されている理由の説明(80〜120文字程度)",
    "candidates": [
      {{"name": "企業名", "note": "その企業がこのテーマに関連する理由(30文字程度)"}}
    ]
  }}
]
"""


def call_gemini(prompt: str, max_retries: int = 6) -> list[dict]:
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    last_resp = None
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(GEMINI_URL, headers=headers, json=body, timeout=90)
        except requests.exceptions.RequestException as e:
            wait = min(2**attempt, 30)
            print(f"[WARN] Gemini通信エラー({e.__class__.__name__})、{wait}秒待って再試行します ({attempt + 1}/{max_retries})")
            last_exc = e
            time.sleep(wait)
            continue

        if resp.ok:
            data = resp.json()
            break

        print(f"[Gemini] {resp.status_code}: {resp.text[:1000]}")
        if resp.status_code in (429, 503):
            wait = min(2**attempt, 30)
            print(f"[WARN] {wait}秒待って再試行します ({attempt + 1}/{max_retries})")
            last_resp = resp
            time.sleep(wait)
            continue
        resp.raise_for_status()
    else:
        if last_resp is not None:
            last_resp.raise_for_status()
        raise last_exc

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def main() -> None:
    headlines = fetch_market_news()
    if not headlines:
        print("ニュース見出しが取得できなかったため中止します。")
        return

    watchlist_names = [JAPANESE_NAMES.get(t, t) for t in WATCHLIST]
    themes = call_gemini(build_prompt(headlines, watchlist_names))

    data = {
        "generatedAt": dt.datetime.now().isoformat(timespec="minutes"),
        "themes": themes,
    }
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[保存しました] {OUT_PATH} ({len(themes)}テーマ)")


if __name__ == "__main__":
    main()
