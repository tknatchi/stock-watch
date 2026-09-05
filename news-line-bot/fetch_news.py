"""
毎朝7時(JST)に、Googleニュースのトップストーリーを取得し、
Gemini(無料枠)でカテゴリ・解説コメントを生成したうえで、
LINE公式アカウントの友だち一人ひとりに、名前入りでpush配信するスクリプト。

必要な環境変数:
  LINE_CHANNEL_ACCESS_TOKEN : LINE Messaging APIのチャンネルアクセストークン(長期)
  GEMINI_API_KEY            : Google AI StudioのGemini APIキー(無料枠)
"""

import os
import re
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

NEWS_RSS_URL = "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"
ARTICLE_COUNT = 5

# 無料枠で使えるGeminiモデル。将来モデル名が変わった場合はここを更新する。
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

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


def call_gemini(prompt):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(GEMINI_URL, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
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


def build_message(display_name, body):
    header = (
        f"おはよう!{display_name}が起きてくる頃には、"
        f"ネット廃人の私はもう今日のニュースを{ARTICLE_COUNT}本厳選し終わってたよ 生意気ですまんな꙳⸌☆⸍꙳\n"
        f"📅 今日の重要ニュース TOP{ARTICLE_COUNT}\n\n"
    )
    return header + body


def get_all_follower_ids():
    """友だち全員のuserIdを取得する(継続トークンでページング)。"""
    ids = []
    cursor = None
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    while True:
        params = {"limit": 1000}
        if cursor:
            params["start"] = cursor
        resp = requests.get(
            "https://api.line.me/v2/bot/followers/ids",
            headers=headers,
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        ids.extend(data.get("userIds", []))
        cursor = data.get("next")
        if not cursor:
            break
    return ids


def get_display_name(user_id):
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    resp = requests.get(
        f"https://api.line.me/v2/bot/profile/{user_id}", headers=headers, timeout=20
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("displayName")


def push_message(user_id, text):
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push", headers=headers, json=body, timeout=20
    )
    if resp.status_code != 200:
        print(f"[WARN] push failed for {user_id}: {resp.status_code} {resp.text}")


def main():
    articles = fetch_top_news()
    if not articles:
        print("記事が取得できなかったため中止します。")
        return

    comments = call_gemini(build_gemini_prompt(articles))
    body = build_body(articles, comments)

    follower_ids = get_all_follower_ids()
    print(f"{len(follower_ids)} 人の友だちに配信します。")

    sent, failed = 0, 0
    for uid in follower_ids:
        name = get_display_name(uid) or "あなた"
        text = build_message(name, body)
        try:
            push_message(uid, text)
            sent += 1
        except requests.RequestException as e:
            print(f"[ERROR] {uid}: {e}")
            failed += 1
        time.sleep(0.1)  # レート制限対策の軽いウェイト

    print(f"完了: 成功 {sent} 件 / 失敗 {failed} 件")


if __name__ == "__main__":
    main()
