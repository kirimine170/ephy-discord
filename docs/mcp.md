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

ephy は認可コードを発行しない．ChatGPT と Grok の認可サーバーは外部 IdP であり，ここはリソースサーバーとして JWT を検証するだけである．ローカルのモックだけ静的 Bearer を受け付ける．

- `EPHY_MCP_BEARER_TOKEN`：ローカル確認用．SHA-256 を比較し，ログへ出さない．
- `EPHY_MCP_OAUTH_ISSUER`：IdP の issuer．文字列として完全一致させ，末尾の `/` も正規化しない．https のみ．
- `EPHY_MCP_OAUTH_AUDIENCE`：公開リソース URL と一致させる．
- `EPHY_MCP_OAUTH_JWKS` または `EPHY_MCP_OAUTH_JWKS_FILE`：運用者が固定した公開鍵．`d` や共通鍵を含むセットは拒否し，URL から鍵を取りに行かない．

受け付ける署名は RS256，ES256，EdDSA．`sub`，`exp`，`iat`，scope `ephy:read`，`azp` または `client_id` を要求する．自己申告の clientInfo は principal ではない．

未認証の `POST /mcp` は 401 である．OAuth が有効なときは次の challenge を返す．

```text
Bearer resource_metadata="<resource>/.well-known/oauth-protected-resource/mcp", scope="ephy:read"
```

メタデータは `/.well-known/oauth-protected-resource` と `/.well-known/oauth-protected-resource/mcp` の両方で返す．authorization server のメタデータは IdP 側にある．

プロトコルはステートレスな JSON である．セッションも SSE も使わない．`MCP-Protocol-Version` は `2026-07-28`，`2025-06-18`，`2025-03-26` を受け付ける．`Host` は `127.0.0.1`，`localhost`，`::1` だけである．トンネルは `--http-host-header 127.0.0.1:8791` を付ける．

`submit_sources` の URL は https だけである．`request_key` が同じで本文が同じ再送は同じ source id を返し，本文が違えば拒否する．この提出は承認ではない．

## トンネル URL の付け替え

quick tunnel のホスト名は再起動で変わる．新しい URL は次で記録する．値は `data/mcp-public-url.json` だけに置き，cloudflared の生ログは残さない．

```bash
python -m ephy_discord mcp-url --config configs/ephy-discord.local.toml set https://NEW.trycloudflare.com
```

記録したあとにプロセスを起動し直す．audience が変わるので，コネクタ側の登録も人が直す．古い JWT は新しい resource では通らない．

## ポート

既定は `8791` である．同じノートのオーケストレータは `127.0.0.1:18640` を使う．8791 を 18640 にすると起動を拒否する．`GET /health` は `{ok, runtime, live_connectors:false}` だけを返す．
