# Security and data handling

## 入れないもの

Git には次を置かない．

- `DISCORD_BOT_TOKEN` とその他の資格情報．見本は `.env.example` の変数名だけである．
- 質問文，回答文，許可チャンネルの通常メッセージ，監査ログ，SQLite．
- Karte の実データ，モデルウェイト．

ローカルの `data/` は Git 管理外である．新規ファイルは可能なら `0600` にする．

## 送信

`llm.base_url` は `http://127.0.0.1`，`http://localhost`，`http://[::1]` だけを受け付ける．ポートは明示する．資格情報，クエリ，フラグメント，HTTPS の外部ホストは拒否する．HTTP クライアントは環境プロキシを使わず，リダイレクトにも従わない．

これにより，設定の書き間違いで Discord の質問が Grok や OpenAI へ出ることを防ぐ．

## 権限

Gateway intent は Guilds，Guild Messages，Message Content だけである．許可チャンネルの通常メッセージを保存するのにも，これ以上の intent は要らない．Bot 権限は View Channels，Send Messages，Read Message History，Use Application Commands だけである．数値は `ephy-discord check` が出す．

`cache.store_all_messages` が true のとき，保存先は許可リストのチャンネルに限る．コネクタへ返す本文は未信頼データであり，承認や権限の変更には使わない．

## 事故

トークンや会話本文をコミットした場合は，配布を止めてトークンを再発行し，履歴からの削除は所有者と調整する．最新ファイルを消すだけでは履歴からは消えない．
