"""
損切り等の重要アラートをLINEに送るための共通モジュール。

news-line-bot/fetch_news.py と同じLINE公式アカウント・broadcast APIを再利用する
(友だちがこのアカウントを見ている前提。宛先を絞りたい場合はpush APIへの変更を検討)。

必要な環境変数:
    LINE_CHANNEL_ACCESS_TOKEN : LINE Messaging APIのチャンネルアクセストークン(長期)
                                 (news-line-botで使っているものと同じでよい)

設計方針:
    アラート送信に失敗しても、呼び出し元の売買提案・シミュレーション処理自体は
    止めない(株価取得や計算が正常に終わっているのに、通知の失敗だけで全体が
    落ちるのは本末転倒なため)。環境変数が未設定の場合も、例外を出さずに
    コンソールへ警告を出すだけにとどめる。
"""

from __future__ import annotations

import os

import requests

LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"


def send_line_alert(text: str) -> bool:
    """LINEにアラートをbroadcast送信する。送れたらTrue、送れなければFalse。"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        print(
            "[LINE alert] LINE_CHANNEL_ACCESS_TOKEN が未設定のため送信をスキップしました。"
            "環境変数を設定すると実際に通知されます。"
        )
        print(f"[LINE alert] 送信予定だった内容:\n{text}")
        return False

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"messages": [{"type": "text", "text": text[:5000]}]}
    try:
        resp = requests.post(LINE_BROADCAST_URL, headers=headers, json=body, timeout=20)
        if not resp.ok:
            print(f"[LINE alert] 送信失敗 {resp.status_code}: {resp.text[:500]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[LINE alert] 送信中にエラー: {e}")
        return False
