"""
毎朝7時(JST)に、Googleニュースのトップストーリーを取得し、
Gemini(無料枠)でカテゴリ・解説コメントを生成したうえで、
LINE公式アカウントの友だち全員にbroadcast配信するスクリプト。

(友だち追加時の「あいさつメッセージ」での名前差し込みはLINE Official Account Manager側の
標準機能でそのまま使えるので、こちらのスクリプトでは扱わない。日々の自動配信は名前なし)

必要な環境変数:
  LINE_CHANNEL_ACCESS_TOKEN : LINE Messaging APIのチャンネルアクセストークン(長期)
  GEMINI_API_KEY            : Google AI StudioのGemini APIキー(無料枠)
"""

import os
import re
import json
import time
import xml.etree.ElementTree as ET

import requests

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

NEWS_RSS_URL = "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"
ARTICLE_COUNT = 5

# モデルのバージョンは明示的に固定する("-latest"エイリアスは無料枠での混雑(503)が起きやすいため避ける)。
# gemini-2.5系は「過去に利用実績のある既存ユーザー限定」で新規プロジェクトからは404になる仕様のため、
# 新規プロジェクトでも使える3.x系を指定している。
# もし404/503になった場合は .github/workflows/debug-gemini.yml (Debug Gemini API Key) を手動実行すると、
# そのキーで使えるモデル一覧が確認できる。
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

CIRCLED_NUMBERS = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]


def fetch_top_news(n=ARTICLE_COUNT):
    """Googleニュース(トップストーリー/日本語)から上位n件を取得する。"""
    resp = requests.get(NEWS_RSS_URL, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = root.findall("./channel/item")

    articles = []
    seen = set()
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        # Googleニュースの見出しは "本文 - 配信元" の形式なので配信元を切り落とす
        clean_title = re.sub(r"\s-\s[^-]+$", "", title).strip()

        # 似た見出し(同じニュースの重複配信)を弾く簡易チェック
        key = clean_title[:15]
        if key in seen:
            continue
        seen.add(key)

        articles.append({"title": clean_title, "link": link})
        if len(articles) >= n:
            break
    return articles


def build_gemini_prompt(articles):
    lines = "\n".join(f"{i + 1}. {a['title']}" for i, a in enumerate(articles))
    return f"""あなたは「ネットに詳しくてちょっと生意気なおじさん」キャラとして、LINE配信用のニュース解説を書きます。
以下の{len(articles)}件のニュース見出しそれぞれについて、次のJSON配列だけを出力してください。

各要素の形式:
- "category": 記事の内容を一言で表すジャンル名(例: 経済、国際情勢、災害・防災、スポーツ、エンタメ など。2〜6文字程度)
- "comment": その記事についての解説文。以下を必ず満たすこと。
  - 文字数は120〜200文字程度
  - 口調は「おじさん構文」(絵文字・記号を多用してテンション高め、馴れ馴れしい)と「生意気構文」(ちょっと上から目線でからかう・煽る)を混ぜたテイストにする
  - ただし内容はふざけすぎず、そのニュースに詳しくない人でも背景や意味がわかるように、易しい言葉で橋渡しする解説を必ず含めること(単なる要約で終わらせない)
  - 絵文字は "꙳⸌☆⸍꙳" のような装飾系の記号を1コメントにつき1〜2個程度使ってよい(使いすぎない)
  - 句読点・「!」・「…」を適度に使い、テンポよく書く

ニュース見出し一覧:
{lines}

出力はJSON配列のみ。前置きや説明文字は一切不要。フォーマット例:
[{{"category": "経済", "comment": "……"}}, {{"category": "国際情勢", "comment": "……"}}]
"""


def call_gemini(prompt, max_retries=6):
    # APIキーはヘッダーで渡す(?key=クエリはログやURL履歴に残りやすいため避ける)。
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            # temperatureは指定しない(Gemini 3系はデフォルトのままの方が安定するため)
        },
    }

    last_resp = None
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(GEMINI_URL, headers=headers, json=body, timeout=90)
        except requests.exceptions.RequestException as e:
            # タイムアウトや接続エラーなど、レスポンス自体が返ってこないケースもリトライする
            wait = min(2 ** attempt, 30)
            print(f"[WARN] Gemini通信エラー({e.__class__.__name__})、{wait}秒待って再試行します ({attempt + 1}/{max_retries})")
            last_exc = e
            time.sleep(wait)
            continue

        if resp.ok:
            data = resp.json()
            break

        # 何が起きているか必ずログに残す(raise_for_statusだけだと理由の本文が消えてしまうため)
        print(f"[Gemini] {resp.status_code}: {resp.text[:1000]}")

        if resp.status_code in (429, 503):
            # 無料枠でよく起きる一時的なエラーなので、待ってリトライする
            wait = min(2 ** attempt, 30)  # 1, 2, 4, 8, 16, 30秒と待ち時間を伸ばす
            print(f"[WARN] {wait}秒待って再試行します ({attempt + 1}/{max_retries})")
            last_resp = resp
            time.sleep(wait)
            continue

        resp.raise_for_status()  # 429/503以外は即エラーにする
    else:
        if last_resp is not None:
            last_resp.raise_for_status()  # 全部失敗したら最後のエラーを投げる
        raise last_exc  # 最後まで通信エラーだった場合
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def build_body(articles, comments):
    blocks = []
    for i, (a, c) in enumerate(zip(articles, comments)):
        number = CIRCLED_NUMBERS[i] if i < len(CIRCLED_NUMBERS) else f"{i + 1}."
        block = (
            f"{number} {a['title']}\n"
            f"🏷 {c.get('category', 'ニュース')}\n"
            f"💡 {c.get('comment', '')}\n"
            f"🔗 {a['link']}"
        )
        blocks.append(block)
    return "\n\n".join(blocks)


def build_message(body):
    header = (
        f"おはよう!今日もネット廃人の私が愛(?)を込めて厳選してきたぜ 生意気ですまんな꙳⸌☆⸍꙳\n"
        f"📅 今日の重要ニュース TOP{ARTICLE_COUNT}\n\n"
    )
    return header + body


def broadcast_message(text):
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {"messages": [{"type": "text", "text": text}]}
    resp = requests.post(
        "https://api.line.me/v2/bot/message/broadcast", headers=headers, json=body, timeout=20
    )
    if not resp.ok:
        print(f"[LINE broadcast] {resp.status_code}: {resp.text[:1000]}")
    resp.raise_for_status()


def main():
    articles = fetch_top_news()
    if not articles:
        print("記事が取得できなかったため中止します。")
        return

    comments = call_gemini(build_gemini_prompt(articles))
    body = build_body(articles, comments)
    text = build_message(body)

    broadcast_message(text)
    print("配信完了")


if __name__ == "__main__":
    main()
