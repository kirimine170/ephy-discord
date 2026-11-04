# Roadmap

状態と構想を分けて書く．日付や完了率は置かない．

## O-1（このリポジトリの実装）

読み取り専用ボット．許可チャンネルのメンションと `/ephy ask` に，Karte の読み取りとローカルモデルだけで答える．外部 API は呼ばない．外へ出る操作は返信だけである．キャッシュ，レート制限，保持期間，削除コマンド，停止，監査を持つ．

## O-2（構想）

Grok（xAI `x_search`）への調査を足す．実行には `ephy-worker` の ResearchJob 契約が要る．対応するタスクは [T-098](https://github.com/kirimine170/ephy-worker/issues/2) である．

T-098 の現状は，オープンな issue と `ephy-worker` の `docs/research-job-v1-contract.md` にある設計案である．同文書は，schema，endpoint，受入，実装のいずれも主張しないと書いている．O-2 はその契約が受入されるまで，このリポジトリから Job を送らない．

## O-2 のあと（構想）

- OpenAI の `web_search` を，Discord の承認ボタンで許可してから使う．
- ephy を MCP サーバーとして出し，Meta Muse への経路にする．
- 返信以外の操作は，承認ボタンが押されたものだけを実行する．

これらは O-1 のコードには無い．`ephy-runtime` が既に持つ Web 検索 plan / approve も，このマイルストーンからは呼ばない．
