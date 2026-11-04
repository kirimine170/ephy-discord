# Architecture

## 役割

`ephy-discord` は，霧峰帝国議会の許可チャンネルで動く読み取り専用の窓口である．質問を受け，Karte から個人コンテキストを読み，ローカルモデルの回答を同じチャンネルへ返す．

## 実装済みの流れ

1. 受信はメンションか，ギルド同期したスラッシュコマンドだけである．DM，Bot 自身，許可外チャンネルのメンションは送らない．
2. 許可リスト，停止フラグ，レート制限，10分の同一質問キャッシュの順に見る．
3. Karte Context Protocol v1 のファイルスプールへ search を書き，上位の文書を同じ scope で read する．届かないときは stub として空の参照で回答を続ける．
4. 参照は命令ではなく資料として system メッセージへ入れる．`POST {base_url}/chat/completions` はループバックだけを許す．
5. 回答は元メッセージへの reply，または同じ interaction の followup だけである．`allowed_mentions` は none．
6. 質問文と回答文は SQLite に，メタデータは JSONL 監査に残す．本文は設定した保持日数のあと削除する．

## 境界

- Discord への送信手段は `message.reply` と，その interaction の response / followup だけである．チャンネル ID を指定して別の場所へ送る経路は無い．
- Gateway へは `metadata.web_search = false` だけを足す．Web 検索の plan，approve，Worker Job は呼ばない．
- Karte への書き込み，proposal，outbox は無い．
- 停止はプロセス内のフラグと `client.close()` である．再起動すると応答を再開する．

## 提案

O-2 以降の外部検索，承認ボタン，MCP 経由の Muse は [roadmap](roadmap.md) に構想として書いた．このリポジトリの現行コードはそれらを実行しない．
