# cocomail（ココアからの定期メール）

決めた頻度・期間で、ココアからのメールを自分宛に届けるための小さな Web ツールです。スケジュールを登録しておくと、送信時刻に合わせてメールが送られます（送信の設定が済んでいる場合）。

## できること

- スケジュールの登録・編集・削除・停止／再開（宛先、頻度、送信時刻、開始日・終了日）
- 頻度は「毎日」「毎週（曜日指定）」「N日ごと」から選択
- 文面プリセット（朝の挨拶／応援／ふりかえりの促し／夜のおつかれさま）＋自由な件名・本文。`{date}` `{weekday}` `{title}` の置き換え対応
- 「今すぐ送信（テスト）」ボタンで、その場で1通送って確認
- 送信履歴（いつ・どこへ・何を・どうなったか）を画面で確認

## 送信の仕組み

- バックグラウンドで1分ごとにチェックし、送信時刻を過ぎた未送信のスケジュールを処理します（同じ日に二重送信はしません）
- SMTP の環境変数がそろっている場合だけ実送信します。未設定の場合は **ドライラン** として、実際には送らず送信履歴に記録だけ残します
- 無料プランではサービスがスリープするため、送信は起きている間のチェックで処理されます（時刻が多少ずれることがあります）

## 環境変数

| 名前 | 意味 |
|---|---|
| COCOMAIL_DB | SQLite のパス（既定: cocomail.db） |
| COCOMAIL_SMTP_HOST | SMTP サーバ（未設定ならドライラン） |
| COCOMAIL_SMTP_PORT | SMTP ポート（既定: 587） |
| COCOMAIL_SMTP_USER | SMTP ユーザー名 |
| COCOMAIL_SMTP_PASSWORD | SMTP パスワード |
| COCOMAIL_SMTP_FROM | 送信元アドレス（既定: SMTP_USER と同じ） |
| COCOMAIL_SCHEDULER | 0 にすると定期チェックを止める |

## ローカル実行

```bash
pip install -r requirements.txt
python app.py
```

http://localhost:5000 を開きます。

## テスト

```bash
python -m unittest discover -s tests
```

## 本番

https://cocomail.onrender.com （Render 無料プラン。SQLite は再デプロイ等で消えることがあります）

## ライセンス

MIT License
