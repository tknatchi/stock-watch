# news-line-bot

毎朝7時(JST)に、Googleニュースのトップストーリーから5件を選び、Gemini(無料枠)でカテゴリ・解説コメントを生成し、
LINE公式アカウントの友だち一人ひとりに「あなたの名前入り」でpush配信するボット。
n8nで運用していたものをGitHub Actionsだけで無料運用できるように置き換えたもの。

このリポジトリ(stock-watch)内の `news-line-bot/` サブフォルダとして同居させている
(既存の株ウォッチ関連スクリプトとは無関係な別プロジェクト)。

## 構成

- `fetch_news.py` … 本体スクリプト(ニュース取得 → Gemini生成 → LINE配信)
- `../.github/workflows/news-line.yml` … 毎日7:00(JST)に自動実行するGitHub Actionsのcron設定(リポジトリ直下に配置)
- `requirements.txt` … 依存パッケージ(requestsのみ)

### なぜbroadcastではなくpushなのか

配信メッセージに「受信者の名前」を入れたいという要件があるため。LINE Official Account Managerの
配信画面にある名前差し込み機能はMessaging APIからは使えないため、代わりに

1. 友だち全員のuserIdを取得(`/v2/bot/followers/ids`)
2. 各userIdのdisplayNameを取得(`/v2/bot/profile/{userId}`)
3. 1人ずつpushメッセージを送信(`/v2/bot/message/push`)

という方式にしている。**Gemini呼び出しは1日1回だけ**(ニュース本文の生成は全員共通)にして、
名前入りの煽り文句は挨拶部分だけPythonの文字列テンプレートで組み立てている。
これにより友だちの人数が増えてもAI側のコスト・呼び出し回数は変わらない。

## セットアップ手順

### 1. Gemini APIキーを取得(無料)

1. https://aistudio.google.com/apikey にアクセス
2. 「Create API key」でキーを発行

無料枠には1日あたり/1分あたりのリクエスト数制限があります。このボットは1日1回しか呼ばないので
通常の無料枠の範囲で問題なく収まります。

### 2. LINEのチャンネルアクセストークンを取得

1. https://developers.line.biz/console/ にログイン
2. 対象のMessaging APIチャンネルを開く
3. 「Messaging API設定」タブ → 「チャンネルアクセストークン(長期)」を発行

### 3. GitHub Secretsを登録(登録済み)

`stock-watch` リポジトリの Settings → Secrets and variables → Actions → New repository secret で、以下を登録:

- `LINE_CHANNEL_ACCESS_TOKEN`
- `GEMINI_API_KEY`

### 4. 動作確認

リポジトリの Actions タブ → `Daily LINE News Broadcast` → `Run workflow` で手動実行し、
自分のLINEに配信メッセージが届くか確認する。

## 既知の注意点

- **GitHub Actionsのスケジュール実行は、リポジトリに60日間まったくアクティビティ(pushなど)がないと自動的に無効化される。** 長期間コードを変更しない場合は、Actionsタブから再度有効化するか、README更新などで定期的にコミットしておくとよい。
- `cron` のスケジュールは負荷状況により数分〜数十分遅延することがある(GitHub側の仕様)。時刻の厳密さが必要な用途には向かない。
- Gemini・LINEとも無料枠には利用上限があるため、仕様(記事数・友だち数)が大きく変わる場合は各サービスの無料枠の範囲を確認すること。
- `/v2/bot/followers/ids` は友だちが多い場合や契約プランによって挙動が異なることがある。初回の手動実行(workflow_dispatch)で403などのエラーが出た場合は、LINE Official Account Managerの契約プラン(メッセージ通数上限含む)を確認する。
- push配信は友だち1人につき1通としてLINEの月間メッセージ通数にカウントされる(broadcastでも同様のため、この移行による追加コストはない)。友だち数が無料メッセージ通数の上限に近い場合は、送信数がプランの上限内に収まるか確認しておく。
