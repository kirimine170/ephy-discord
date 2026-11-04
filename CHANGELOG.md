# Changelog

## 0.2.0

コネクタ向けの MCP（既定は無効）と，Discord の承認ボタンを追加した．MCP は Karte の search/read，許可チャンネルの保存済み会話，ローカルモデルへの質問，SNS 発見のローカル保存を提供する．発見は Karte に書かない．承認後に実行するのは許可チャンネルへの返信だけで，他の操作は記録して止める．Windows では DPAPI と起動スクリプト，待受ポート 8791 を使う．

## 0.1.0

O-1 の読み取り専用 Discord アダプタ．メンションと `/ephy ask` は，Karte Context Protocol v1 の参照と，ループバックの OpenAI 互換チャット（ephy-runtime Gateway または llama.cpp）だけで答える．SQLite の重複排除，レート制限，7日の本文削除，`/ephy forget`，`/ephy purge`，`/ephy stop`，JSONL 監査を含む．
