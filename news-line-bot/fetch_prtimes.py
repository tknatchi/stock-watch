"""
PR TIMESの「ヘッドラインメール」(info@prtimes.jpから1日数回届く、その時間帯に配信された
全プレスリリース見出しをまとめたダイジェストメール)がGmailに届くたびに検知し、
先頭ARTICLE_COUNT件をGemini(無料枠)で解説したうえで、LINE公式アカウントの友だち全員に
broadcast配信するスクリプト。

15分おきに実行されるワークフローから毎回呼び出される想定。実行のたびに
「未読(UNSEEN)かつinfo@prtimes.jpから届いたメール」をIMAPで検索し、見つかった最古の1通だけを
処理する(1回の実行で処理するのは常に1通。複数溜まっていても取りこぼさないよう、次回の実行で
残りを拾う)。処理したメールはこの中で既読(\\Seen)にすることで、次回以降の検索対象から外し、
二重配信を防ぐ。

ヘッドラインメール1通には多い時で80件以上のプレスリリース見出しが並んでいるため、
全件は解説しない。メール内の並び順(ほぼ発表時刻順)で先頭からARTICLE_COUNT件だけを機械的に
抜き出す。

必要な環境変数:
  GMAIL_ADDRESS             : IMAPでログインするGmail(Google Workspace)アドレス
  GMAIL_APP_PASSWORD        : 上記アカウントのアプリパスワード(2段階認証を有効にした上で発行)
  LINE_CHANNEL_ACCESS_TOKEN : LINE Messaging APIのチャンネルアクセストークン(長期)
  GEMINI_API_KEY            : Google AI StudioのGemini APIキー(無料枠)
"""

import os
import re
import json
import time
import html
import imaplib
import email
from email.header import decode_header

import requests

GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

IMAP_HOST = "imap.gmail.com"
PRTIMES_SENDER = "info@prtimes.jp"
ARTICLE_COUNT = 5  # 1通のメールから先頭何件を取り上げるか

# fetch_news.py と同じ理由(無料枠での混雑・新規プロジェクトでの404回避)でモデル名を明示的に固定する。
# 404/503になった場合は .github/workflows/debug-gemini.yml (Debug Gemini API Key) を手動実行すると、
# そのキーで使えるモデル一覧が確認できる。
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

CIRCLED_NUMBERS = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]

# PR TIMESヘッドラインメールは、記事1件ごとに以下の2つの要素が並んで出てくる:
#   1. 「発表元企業名 + 日付」の行
#   2. 「連番 + 見出しテキスト」がそのままリンクになった行(hrefはprtimes.jp/main/html/rd/p/...)
# メール本文のHTMLはタグの閉じ忘れ等で構造が崩れている(実際に届いたメールで確認済み)ため、
# 厳密なHTMLパーサではなく正規表現で拾い、出現順にzipして対応づける。
COMPANY_RE = re.compile(
    r'<td style="padding-bottom:12px; font-size:13px; color:#a2a2a2;">\s*(.+?)\s{2,}(\d{4}-\d{2}-\d{2})\s*</td>',
    re.DOTALL,
)
HEADLINE_RE = re.compile(
    r'<a href="(https://prtimes\.jp/main/html/rd/p/[^"]+)"[^>]*font-size:22px[^>]*>\s*(\d+)\.\s*(.*?)</a>',
    re.DOTALL,
)


def decode_mime_subject(raw_subject):
    parts = decode_header(raw_subject or "")
    return "".join(
        (part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part)
        for part, enc in parts
    )


def clean_title(raw_title):
    """記事タイトルの断片からHTMLタグ/エンティティ/余分な空白・改行を取り除く。"""
    text = re.sub(r"<[^>]+>", " ", raw_title)  # <br />などのタグを除去
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_latest_prtimes_html():
    """
    未読かつinfo@prtimes.jpから届いたメールのうち一番古い1通を取得し、本文HTMLを返す。
    見つからなければNoneを返す。処理対象にしたメールはこの関数の中で既読にする。
    """
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX")

        status, data = imap.search(None, f'(UNSEEN FROM "{PRTIMES_SENDER}")')
        if status != "OK":
            raise RuntimeError(f"IMAP検索に失敗しました: {status}")

        ids = data[0].split()
        if not ids:
            return None

        # 一番古い未読メールから順に1通ずつ処理する(取りこぼしを避けるため)
        msg_id = ids[0]
        status, msg_data = imap.fetch(msg_id, "(RFC822)")
        if status != "OK":
            raise RuntimeError(f"IMAP fetchに失敗しました: {status}")

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        html_body = None
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    charset = part.get_content_charset() or "utf-8"
                    html_body = part.get_payload(decode=True).decode(charset, errors="replace")
                    break
        else:
            charset = msg.get_content_charset() or "utf-8"
            html_body = msg.get_payload(decode=True).decode(charset, errors="replace")

        # 既読にする(次回以降の検索対象から外れる = 二重配信防止)。
        # 処理に失敗して例外が飛んでも既読にはなるが、同じメールを延々リトライされて
        # ハマるよりは「1通取りこぼす」方が安全という判断。
        imap.store(msg_id, "+FLAGS", "\\Seen")

        subject = decode_mime_subject(msg.get("Subject"))
        print(f"[INFO] 処理対象メール: {subject}")
        return html_body
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def parse_articles(html_body, n=ARTICLE_COUNT):
    companies = COMPANY_RE.findall(html_body)
    headlines = HEADLINE_RE.findall(html_body)

    if len(companies) != len(headlines):
        # ズレていても位置対応を諦めず、短い方に合わせて処理は続行する(全滅させないため)
        print(f"[WARN] 企業名({len(companies)}件)と見出し({len(headlines)}件)の件数が一致しません")

    articles = []
    for (company, _date), (link, _num, raw_title) in zip(companies, headlines):
        title = clean_title(raw_title)
        if not title:
            continue
        articles.append({"title": title, "link": link, "company": company.strip()})
        if len(articles) >= n:
            break
    return articles


def build_gemini_prompt(articles):
    lines = "\n".join(f"{i + 1}. [{a['company']}] {a['title']}" for i, a in enumerate(articles))
    return f"""あなたは「ネットに詳しくてちょっと生意気なおじさん」キャラとして、LINE配信用に企業のプレスリリースの解説を書きます。
以下の{len(articles)}件のプレスリリース見出し(発表元企業名つき)それぞれについて、次のJSON配列だけを出力してください。

各要素の形式:
- "category": リリースの内容を一言で表すジャンル名(例: 新商品、資金調達、コラボ、イベント、採用、店舗展開 など。2〜6文字程度)
- "comment": そのリリースについての解説文。以下を必ず満たすこと。
  - 文字数は120〜200文字程度
  - 口調は「おじさん構文」(絵文字・記号を多用してテンション高め、馴れ馴れしい)と「生意気構文」(ちょっと上から目線でからかう・煽る)を混ぜたテイストにする
  - ただし内容はふざけすぎず、そのリリースに詳しくない人でも「どの会社が何をしたのか」がひと目でわかるように、易しい言葉で橋渡しする解説を必ず含めること(見出しの繰り返しで終わらせない)
  - 絵文字は "꙳⸌☆⸍꙳" のような装飾系の記号を1コメントにつき2〜4個程度、文中に散りばめて使う
  - 読みやすさのため、文章の途中1〜2箇所に改行(\\n)を入れて2〜3行程度に分けること
  - 句読点・「!」・「…」を適度に使い、テンポよく書く
  - 文末は「〜だよ!」のような可愛らしい言い方は絶対に使わない。「〜なんだぜ( ¯ ꒳¯)ﾄﾞﾔｧ」「〜ってわけだ」「〜なんだよなぁ」のような、決め顔で自慢げな"おじさん構文"の語尾を使うこと

全体を通しての追加指示:
- {len(articles)}件のcomment全体のうち、1〜2件だけに、その内容に絡めた「おやじギャグ」(ダジャレ)を自然に混ぜ込むこと。残りには無理にダジャレを入れないこと(全部に入れるとしつこくなるため)

プレスリリース見出し一覧(発表元企業名つき):
{lines}

出力はJSON配列のみ。前置きや説明文字は一切不要。フォーマット例:
[{{"category": "新商品", "comment": "……"}}, {{"category": "コラボ", "comment": "……"}}]
"""


def call_gemini(prompt, max_retries=6):
    # APIキーはヘッダーで渡す(?key=クエリはログやURL履歴に残りやすいため避ける)。
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }

    last_resp = None
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(GEMINI_URL, headers=headers, json=body, timeout=90)
        except requests.exceptions.RequestException as e:
            wait = min(2 ** attempt, 30)
            print(f"[WARN] Gemini通信エラー({e.__class__.__name__})、{wait}秒待って再試行します ({attempt + 1}/{max_retries})")
            last_exc = e
            time.sleep(wait)
            continue

        if resp.ok:
            data = resp.json()
            break

        print(f"[Gemini] {resp.status_code}: {resp.text[:1000]}")

        if resp.status_code in (429, 503):
            wait = min(2 ** attempt, 30)
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


def build_body(articles, comments):
    blocks = []
    for i, (a, c) in enumerate(zip(articles, comments)):
        number = CIRCLED_NUMBERS[i] if i < len(CIRCLED_NUMBERS) else f"{i + 1}."
        block = (
            f"{number} 【{a['company']}】{a['title']}\n"
            f"🏷 {c.get('category', 'PR')}\n"
            f"💡 {c.get('comment', '')}\n"
            f"🔗 {a['link']}"
        )
        blocks.append(block)
    return "\n\n".join(blocks)


def build_message(body, count):
    header = (
        f"おっ、PR TIMESから新着が届いたぞ꙳⸌☆⸍꙳ 気になる企業のリリースを厳選してきてやったから、"
        f"ちゃんとチェックしとけよ꙳⸌☆⸍꙳\n"
        f"📰 PR TIMES ヘッドラインピックアップ TOP{count}\n\n"
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
    html_body = fetch_latest_prtimes_html()
    if html_body is None:
        print("新着のPR TIMESヘッドラインメールはありませんでした。終了します。")
        return

    articles = parse_articles(html_body)
    if not articles:
        print("メールから記事を抽出できなかったため中止します。")
        return

    comments = call_gemini(build_gemini_prompt(articles))
    body = build_body(articles, comments)
    text = build_message(body, len(articles))

    broadcast_message(text)
    print("配信完了")


if __name__ == "__main__":
    main()
