"""
fetch_prtimes.py のメール本文パース処理(parse_articles)の回帰テスト。

fixtures/sample_prtimes_headline.html は、実際にPR TIMESヘッドラインメールとして
届いたHTMLをそのまま抜粋・保存したもの(件数を絞るため一部の記事ブロックのみ残しているが、
タグの崩れ方などの構造は本物のまま)。
"""

import os
import sys
import unittest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# fetch_prtimes.py はモジュール読み込み時に必須の環境変数を要求する(secret未設定でのCI実行を
# 早期に落とすための意図的な仕様。fetch_news.py と同じ設計)。テスト実行時は実際の値は使わないので、
# import前にダミー値を入れておく。
os.environ.setdefault("GMAIL_ADDRESS", "dummy@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "dummy")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")

import fetch_prtimes as fp
import requests

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample_prtimes_headline.html")


def load_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return f.read()


class ParseArticlesTests(unittest.TestCase):
    def setUp(self):
        self.html = load_fixture()

    def test_extracts_default_article_count_from_head_of_list(self):
        # fixtureには8件分のブロックが入っているが、既定のARTICLE_COUNT(5件)だけ返る
        articles = fp.parse_articles(self.html)
        self.assertEqual(len(articles), 5)

    def test_first_article_matches_the_image_sponsored_slot(self):
        # 1件目は画像付きスロット(画像用の<a>タグが別にある)だが、見出しリンクだけが拾われる
        articles = fp.parse_articles(self.html, n=1)
        a = articles[0]
        self.assertEqual(a["company"], "株式会社LEVECHY")
        self.assertEqual(a["link"], "https://prtimes.jp/main/html/rd/p/000000173.000037420.html")
        self.assertIn("LEVECHY Lending", a["title"])

    def test_titles_are_html_unescaped_and_whitespace_normalized(self):
        # 37番目はタイトル中に&amp;と改行を含む(本物のメールでも見られた崩れ方)
        articles = fp.parse_articles(self.html, n=100)
        by_company = {a["company"]: a for a in articles}
        target = by_company["京山幸太事務局"]
        self.assertNotIn("&amp;", target["title"])
        self.assertIn("東京&大阪", target["title"])
        self.assertNotIn("\n", target["title"])

    def test_multiline_title_is_joined_into_a_single_line(self):
        # 14番目はタイトルが2行にまたがっている(実物メールでも複数件で見られた)
        articles = fp.parse_articles(self.html, n=100)
        by_company = {a["company"]: a for a in articles}
        target = by_company["株式会社コスギ不動産ホールディングス"]
        self.assertNotIn("\n", target["title"])
        self.assertIn("インターンシップ開催報告", target["title"])
        self.assertIn("まちづくりまで体験できるプログラム", target["title"])

    def test_n_can_request_fewer_than_default(self):
        articles = fp.parse_articles(self.html, n=2)
        self.assertEqual(len(articles), 2)

    def test_returns_empty_list_for_html_with_no_articles(self):
        self.assertEqual(fp.parse_articles("<html><body>no articles here</body></html>"), [])

    def test_continues_with_shorter_list_when_company_and_headline_counts_differ(self):
        # 見出しリンクが1つ多い(対応する会社名ブロックが欠けている)壊れたHTML。
        # 実物メールでも稀にテンプレートが崩れることがあるため、クラッシュせず
        # 短い方(会社名ブロックの件数)に合わせて処理を続けることを確認する。
        broken_html = """
        <td style="padding-bottom:12px; font-size:13px; color:#a2a2a2;">
        株式会社X    2026-09-28</td>
        <a href="https://prtimes.jp/main/html/rd/p/000000001.000000001.html" style="font-size:22px; display:block;">
        1. 一件目の見出し</a>
        <a href="https://prtimes.jp/main/html/rd/p/000000002.000000002.html" style="font-size:22px; display:block;">
        2. 二件目の見出し(対応する会社名ブロックが無い)</a>
        """
        articles = fp.parse_articles(broken_html, n=10)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["company"], "株式会社X")


class CleanTitleTests(unittest.TestCase):
    def test_strips_tags_and_collapses_whitespace(self):
        raw = "見出し<br />\n本文が   続く"
        self.assertEqual(fp.clean_title(raw), "見出し 本文が 続く")

    def test_unescapes_html_entities(self):
        self.assertEqual(fp.clean_title("A&amp;B &quot;C&quot;"), 'A&B "C"')


class DecodeMimeSubjectTests(unittest.TestCase):
    def test_decodes_mime_encoded_subject(self):
        raw = (
            "=?utf-8?B?44CQUFIgVElNRVMg44OY44OD44OJ44Op44Kk44Oz44CRIDIwMjYtMDktMjg=?= "
            "=?utf-8?B?IOaYvOWIig==?="
        )
        self.assertEqual(fp.decode_mime_subject(raw), "【PR TIMES ヘッドライン】 2026-09-28 昼刊")

    def test_plain_ascii_subject_is_unchanged(self):
        self.assertEqual(fp.decode_mime_subject("Plain Subject"), "Plain Subject")

    def test_none_subject_does_not_raise(self):
        self.assertEqual(fp.decode_mime_subject(None), "")


class BuildBodyTests(unittest.TestCase):
    def test_falls_back_to_plain_numbering_beyond_circled_numbers_range(self):
        # CIRCLED_NUMBERSは①〜⑩の10件分しか無いので、11件目以降は "11." 形式にフォールバックする
        articles = [{"title": f"t{i}", "link": f"https://x/{i}", "company": f"c{i}"} for i in range(11)]
        comments = [{"category": "cat", "comment": "com"} for _ in range(11)]
        body = fp.build_body(articles, comments)
        self.assertIn("⑩ 【c9】t9", body)
        self.assertIn("11. 【c10】t10", body)


class BuildMessageTests(unittest.TestCase):
    def test_header_includes_the_article_count(self):
        msg = fp.build_message("本文プレースホルダ", 7)
        self.assertIn("TOP7", msg)
        self.assertTrue(msg.endswith("本文プレースホルダ"))


def make_plain_html_email(html_body, subject="Subject"):
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = "info@prtimes.jp"
    return msg.as_bytes()


def make_multipart_html_email(html_body, plain_body="plain fallback", subject="Subject"):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = "info@prtimes.jp"
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg.as_bytes()


class FakeIMAP:
    """imaplib.IMAP4_SSL の最小限の偽実装。search/fetch/storeの呼ばれ方だけを検証する。"""

    def __init__(self, search_ids=(), messages=None, search_status="OK"):
        self.search_status = search_status
        self.search_ids = search_ids
        self.messages = messages or {}
        self.store_calls = []
        self.logged_out = False

    def login(self, user, password):
        return ("OK", [])

    def select(self, mailbox):
        return ("OK", [])

    def search(self, charset, criterion):
        return (self.search_status, [b" ".join(self.search_ids)])

    def fetch(self, msg_id, parts):
        return ("OK", [(None, self.messages[msg_id])])

    def store(self, msg_id, flag_cmd, flags):
        self.store_calls.append((msg_id, flag_cmd, flags))
        return ("OK", [])

    def logout(self):
        self.logged_out = True


class FetchLatestPrtimesHtmlTests(unittest.TestCase):
    def test_returns_none_and_does_not_mark_anything_seen_when_no_unread_mail(self):
        fake = FakeIMAP(search_ids=())
        with mock.patch.object(fp.imaplib, "IMAP4_SSL", return_value=fake):
            result = fp.fetch_latest_prtimes_html()
        self.assertIsNone(result)
        self.assertEqual(fake.store_calls, [])
        self.assertTrue(fake.logged_out)

    def test_returns_html_body_and_marks_the_message_seen_when_unread_mail_found(self):
        raw = make_plain_html_email("<html>plain single-part body</html>")
        fake = FakeIMAP(search_ids=(b"1",), messages={b"1": raw})
        with mock.patch.object(fp.imaplib, "IMAP4_SSL", return_value=fake):
            result = fp.fetch_latest_prtimes_html()
        self.assertIn("plain single-part body", result)
        self.assertEqual(fake.store_calls, [(b"1", "+FLAGS", "\\Seen")])

    def test_extracts_text_html_part_from_a_multipart_message(self):
        raw = make_multipart_html_email("<html>multipart html body</html>")
        fake = FakeIMAP(search_ids=(b"7",), messages={b"7": raw})
        with mock.patch.object(fp.imaplib, "IMAP4_SSL", return_value=fake):
            result = fp.fetch_latest_prtimes_html()
        self.assertIn("multipart html body", result)
        self.assertNotIn("plain fallback", result)

    def test_raises_when_imap_search_fails(self):
        fake = FakeIMAP(search_ids=(), search_status="NO")
        with mock.patch.object(fp.imaplib, "IMAP4_SSL", return_value=fake):
            with self.assertRaises(RuntimeError):
                fp.fetch_latest_prtimes_html()


class FakeGeminiResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")


class CallGeminiTests(unittest.TestCase):
    def setUp(self):
        # 429/503のリトライ待ち(最大30秒)でテストを遅くしないようにする
        patcher = mock.patch.object(fp.time, "sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_on_503_then_succeeds(self):
        ok_json = {
            "candidates": [
                {"content": {"parts": [{"text": '[{"category": "a", "comment": "b"}]'}]}}
            ]
        }
        responses = [FakeGeminiResponse(503, text="busy"), FakeGeminiResponse(200, json_data=ok_json)]
        with mock.patch.object(fp.requests, "post", side_effect=responses):
            result = fp.call_gemini("prompt", max_retries=3)
        self.assertEqual(result, [{"category": "a", "comment": "b"}])

    def test_raises_after_exhausting_retries_on_503(self):
        responses = [FakeGeminiResponse(503, text="busy") for _ in range(3)]
        with mock.patch.object(fp.requests, "post", side_effect=responses):
            with self.assertRaises(requests.exceptions.HTTPError):
                fp.call_gemini("prompt", max_retries=3)

    def test_raises_immediately_on_non_retryable_error(self):
        # 400等は即エラーで、429/503のようにリトライしない。
        # (side_effectを1件しか用意していないので、もしリトライしてしまうと
        #  StopIterationになりこのテスト自体が失敗する)
        responses = [FakeGeminiResponse(400, text="bad request")]
        with mock.patch.object(fp.requests, "post", side_effect=responses):
            with self.assertRaises(requests.exceptions.HTTPError):
                fp.call_gemini("prompt", max_retries=5)


if __name__ == "__main__":
    unittest.main()
