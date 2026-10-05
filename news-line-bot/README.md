# news-line-bot

LINE公式アカウントの友だち全員にニュースをbroadcast配信するボット2本立て。
n8nで運用していたものをGitHub Actionsだけで無料運用できるように置き換えたもの。

このリポジトリ(stock-watch)内の `news-line-bot/` サブフォルダとして同居させている
(既存の株ウォッチ関連スクリプトとは無関係な別プロジェクト)。

## 構成

### ① Googleニュース版(`fetch_news.py`)

毎朝7時(JST)に、Googleニュースのトップストーリーから5件を選び、Gemini(無料枠)でカテゴリ・解説コメントを生成し配信する。

- `fetch_news.py` … 本体スクリプト(ニュース取得 → Gemini生成 → LINE broadcast配信)
- `../.github/workflows/news-line.yml` … 毎日7:00(JST)に自動実行するGitHub Actionsのcron設定(リポジトリ直下に配置)

### ② PR TIMESヘッドライン版(`fetch_prtimes.py`)

Gmail(Google Workspace)に`info@prtimes.jp`から1日数回届く「PR TIMES ヘッドラインメール」
(その時間帯に配信された全プレスリリース見出しのダイジェスト。多い時で1通80件以上)を検知し、
メール内の並び順で先頭5件だけをGemini(無料枠)で解説して配信する。

- `fetch_prtimes.py` … 本体スクリプト(IMAPで新着メール検知 → 本文パース → Gemini生成 → LINE broadcast配信)
- `../.github/workflows/prtimes-line.yml` … 15分おきに新着メールの有無を確認するGitHub Actionsのcron設定(リポジトリ直下に配置)
- `tests/` … `parse_articles`(メール本文パース)の回帰テスト。実物メールから抜粋したHTML fixtureを使用。`cd news-line-bot && python tests/test_fetch_prtimes.py` で実行

**二重配信防止の仕組み**: 処理したメールはIMAPで既読(`\Seen`)にする。次回以降の実行は
「未読かつinfo@prtimes.jpから届いたメール」だけを検索するので、同じメールを二度処理しない。
裏を返すと、bot以外で(Gmailアプリなどから)先にそのメールを既読にしてしまうと処理をスキップする
(ニュース解説用途なので実害は小さい前提)。

### 共通

- `list_gemini_models.py` … Gemini APIキーの動作確認用デバッグスクリプト
- `../.github/workflows/debug-gemini.yml` … 上記デバッグスクリプトを手動実行するワークフロー
- `requirements.txt` … 依存パッケージ(requestsのみ。IMAP/メール解析はPython標準ライブラリの`imaplib`/`email`で完結)

### 「あいさつメッセージ」の名前差し込みについて

友だち追加時の一回きりの「あいさつメッセージ」でユーザー名を差し込みたい場合は、LINE Official Account
Manager の「あいさつメッセージ」設定画面にある標準の差し込みタグ機能を使えばよい(GUIで完結する)。
これはMessaging API(このリポジトリのコード)とは無関係に、LINE側だけで完結する設定。

一方、**日々の自動配信(broadcast)には名前差し込みを行っていない**。理由は、broadcast配信で
受信者ごとに名前を変えるには「友だち一人ひとりにpushメッセージを送る」実装が必要になるが、
その前提となる「友だち全員のuserId一覧取得」API(`/v2/bot/followers/ids`)は**認証済み/プレミアム
アカウント限定**の機能であり、通常の未認証アカウントでは `403 Forbidden`
(`Access to this API is not available for your account`)になるため。

## セットアップ手順

### 1. Gemini APIキーを取得(無料)

1. https://aistudio.google.com/apikey にアクセス
2. 「APIキーを作成」で新規キーを発行(作成直後の画面でコピーする。一覧の省略表示からのコピーは失敗しやすい)

無料枠には1日あたり/1分あたりのリクエスト数制限があります。①のGoogleニュース版は1日1回、
②のPR TIMES版は新着メールを検知した時だけ(1日数回程度)しかGeminiを呼ばないので、
どちらも通常の無料枠の範囲で問題なく収まります。

モデルのバージョンは頻繁に変わる(廃止・新規ユーザー制限など)ため、404/503が出た場合は
`Debug Gemini API Key` ワークフローを手動実行して、そのキーで使えるモデル一覧を確認すること。

### 2. LINEのチャンネルアクセストークンを取得

1. https://developers.line.biz/console/ にログイン
2. 対象のMessaging APIチャンネルを開く
3. 「Messaging API設定」タブ → 「チャンネルアクセストークン(長期)」を発行

### 3. (②のみ)PR TIMESヘッドラインメールを受信するGmailのアプリパスワードを発行

②のPR TIMES版は、PR TIMESのヘッドラインメールが届くGmail(Google Workspace)アカウントに
IMAPでログインするため、通常のログインパスワードとは別の「アプリパスワード」が必要。

1. 対象のGoogleアカウントで**2段階認証を有効化**する(未設定だとアプリパスワードは発行できない)
2. https://myaccount.google.com/apppasswords でアプリパスワードを発行し、表示された16桁の文字列を控える
3. Gmailの「設定」→「メール転送とPOP/IMAP」で**IMAPを有効にする**
4. Google Workspaceの場合、組織の管理者側でIMAPアクセスやアプリパスワードの発行がポリシーで
   制限されている場合がある。上記の操作ができない/エラーになる場合は管理者に確認すること

### 4. GitHub Secretsを登録

`stock-watch` リポジトリの Settings → Secrets and variables → Actions → New repository secret で、以下を登録:

- `LINE_CHANNEL_ACCESS_TOKEN` … ①②共通
- `GEMINI_API_KEY` … ①②共通
- `GMAIL_ADDRESS` … ②のみ。PR TIMESヘッドラインメールが届くGmailアドレス
- `GMAIL_APP_PASSWORD` … ②のみ。手順3で発行した16桁のアプリパスワード

### 5. 動作確認

- ①: Actions タブ → `Daily LINE News Broadcast` → `Run workflow` で手動実行し、自分のLINEに配信メッセージが届くか確認する
- ②: Actions タブ → `PR TIMES Headline LINE Broadcast` → `Run workflow` で手動実行する。
  未読のPR TIMESヘッドラインメールが受信箱にある状態で実行すると配信され、
  なければ「新着のPR TIMESヘッドラインメールはありませんでした。」とログに出て何も送られない
  (再度試すには、そのメールを一旦未読に戻してから実行する)

## 既知の注意点

- **GitHub Actionsのスケジュール実行は、リポジトリに60日間まったくアクティビティ(pushなど)がないと自動的に無効化される。** 長期間コードを変更しない場合は、Actionsタブから再度有効化するか、README更新などで定期的にコミットしておくとよい。
- `cron` のスケジュールは負荷状況により数分〜数十分遅延することがある(GitHub側の仕様)。時刻の厳密さが必要な用途には向かない。
- Gemini・LINEとも無料枠には利用上限があるため、仕様(記事数・友だち数)が大きく変わる場合は各サービスの無料枠の範囲を確認すること。
- Geminiのモデル名は今後も廃止・世代交代が起きうる。404/503が出た場合は `Debug Gemini API Key` ワークフローでモデル一覧を確認し、`fetch_news.py` / `fetch_prtimes.py` の `GEMINI_MODEL` を更新する。
- ②のPR TIMES版は本文をHTMLの見た目(タグの並び)に依存した正規表現でパースしている。PR TIMES側が
  メールテンプレートの見た目を変更すると抽出できなくなる可能性がある。その場合は
  `news-line-bot/tests/fixtures/sample_prtimes_headline.html` を最新のメールソースで差し替えて
  `fetch_prtimes.py` の `COMPANY_RE`/`HEADLINE_RE` を調整し、`tests/test_fetch_prtimes.py` が
  通ることを確認する。
- ②は15分おきにワークフローが起動するが、実際にGmail/Gemini/LINEへの通信が発生するのは
  新着メールを検知した実行だけ(新着がなければIMAP検索だけして即終了する)。
