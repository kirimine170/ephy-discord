# MCP コネクタ

ChatGPT のカスタム MCP，Grok のカスタムコネクタ，Meta Muse のカスタムコネクタは，それぞれのチャット画面から人が呼び出す．購読の範囲で動き，ephy が相手の API を叩く費用は発生しない．ephy から Grok や OpenAI へ検索を投げる経路ではない．

待受は `[mcp] enabled = false` が既定である．有効にしても `127.0.0.1`，`localhost`，`::1` 以外では待たない．

## ツール

| 名前 | 内容 |
| --- | --- |
| `karte_search` | Context Protocol v1 の search．読み取りだけ |
| `karte_read` | 同じプロトコルの read |
| `discord_summary` | 許可チャンネルに保存した会話の要約．許可外の行は返さない |
| `ask_ephy` | ローカルモデルに Karte の参照付きで聞く．Discord へは投稿しない |
| `submit_sources` | SNS の発見（本文と URL）をローカルキャッシュへ出典付きで置く．Karte には書かない |

`submit_sources` の provenance はコネクタ名（`chatgpt`，`grok`，`muse`，`other`）と，トークンそのものではないクライアント区分である．Karte への採用は Runtime の仕事であり，T-098 の契約が受入されるまで行わない．

## 認証

環境変数だけに置く．

- `EPHY_MCP_BEARER_TOKEN`：静的な Bearer．コネクタが API キーだけを受け付けるときに使う．
- `EPHY_MCP_OAUTH_SECRET`：認可画面の合言葉．OAuth 2.1 の authorization code と PKCE S256．

どちらも空のまま `enabled = true` にすると，Discord ボットは起動しても MCP は待受しない．

公開メタデータは次のとおり．

- `GET /.well-known/oauth-protected-resource`
- `GET /.well-known/oauth-authorization-server`
- `POST /oauth/register`
- `GET|POST /oauth/authorize`
- `POST /oauth/token`
- `POST /mcp`

`mcp.public_base_url` にトンネルの `https` URL を書くと，メタデータはその URL を返す．空なら `http://127.0.0.1:8791` になる．この URL は Git に入れない．

## ポート

既定は `8791` である．この環境からは参照リポジトリの待受ポートを読めなかったので，3000，8080，8787 と重なりにくい番号にしてある．霧峰帝国議会のオーケストレータと重なるときは toml の `mcp.port` を変える．
