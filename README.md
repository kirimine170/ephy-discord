# ephy-discord

ephy の Discord アダプタである．O-1 は読み取り専用ボットで，霧峰帝国議会の許可チャンネルにだけ返事をする．回答はローカルモデルと，Karte から読んだ個人コンテキストだけを使う．

## これは何か

しずく霧峰のローカル常駐 AI，ephy を Discord から使う入口である．メンションか `/ephy ask` を受けると，次の順で答える．

1. チャンネルが toml の許可リストにあることを確認する．
2. 同一の質問が `cache.dedupe_seconds`（既定 10 分）以内にあれば，その回答を再利用する．
3. 無ければ Karte Context Protocol v1 で search し，上位の文書を read する．Karte が無ければ stub として，個人コンテキスト無しで答える．
4. ephy-runtime の Gateway，または llama.cpp の OpenAI 互換 `/v1/chat/completions` へ送る．URL はループバックだけを許す．

Grok，OpenAI，その他の外部 API は呼ばない．コネクタ（ChatGPT，Grok，Muse）からの呼び出しは，人がそれぞれのチャット画面で始めたときだけ MCP で受ける．詳しくは [docs/mcp.md](docs/mcp.md)．

承認が必要な外向き操作は [docs/approval.md](docs/approval.md)．今実行するのは，承認された返信だけである．

GTX 1050 の Windows ノートで議会ボットと並べる手順は [docs/windows.md](docs/windows.md)．MCP の既定ポートは `8791` である．

`ephy-worker` の `discord-runtime` は，Runtime の公開検索へ質問を渡す別の transport である．このリポジトリはそれを起動しない．Discord アプリケーションは分ける．

## ロードマップの位置

実装済みは O-1 だけである．構想は [docs/roadmap.md](docs/roadmap.md) に分けて書いた．

- O-1: このボット．ローカルモデルと Karte の読み取り．
- O-2: Grok の `x_search`．[ephy-worker T-098](https://github.com/kirimine170/ephy-worker/issues/2) の ResearchJob 契約が受入されてから接続する．現在の T-098 は設計案であり，このリポジトリは Job を送らない．
- その後の構想: OpenAI `web_search`，ephy を MCP として Muse へ出すこと，外向きの操作を Discord の承認ボタンで許可すること．

## セットアップ

Python 3.12 以上．

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

トークンは環境変数だけに置く．値をファイルへ書かない．

```bash
export DISCORD_BOT_TOKEN="（Developer Portal の Bot token）"
```

## Discord アプリケーション

1. [Discord Developer Portal](https://discord.com/developers/applications) で New Application を作る．
2. Bot を追加する．Privileged Gateway Intents は Message Content Intent だけをオンにする．Presence Intent と Server Members Intent はオフのままにする．
3. OAuth2 URL Generator で scope に `bot` と `applications.commands` を選ぶ．Bot Permissions は次の4つだけにする．
   - View Channels
   - Send Messages
   - Read Message History
   - Use Application Commands
4. 権限の整数は `2147552256` である．招待 URL の形は次のとおり．`APPLICATION_ID` を置き換える．

```text
https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&scope=bot%20applications.commands&permissions=2147552256
```

5. 霧峰帝国議会へ招待する．
6. サーバー ID と，返事を許すチャンネル ID を開発者モードでコピーし，設定 toml の `guild_id` と `allowed_channel_ids` へ書く．例の `1` はプレースホルダであり，本番の ID ではない．
7. スラッシュコマンドは起動時にそのギルドへだけ同期する．

空の `allowed_channel_ids` ではメンションにも `/ephy ask` にも答えない．

## ローカルモデル

ephy-runtime の Gateway は既定で `http://127.0.0.1:8000` である．設定の `llm.base_url` には `/v1` まで含める．

```toml
[llm]
base_url = "http://127.0.0.1:8000/v1"
model = "auto"
via_gateway = true
gateway_mode = "fast"
```

`via_gateway = true` のとき，本体は `metadata.mode` と `metadata.web_search = false` を付ける．Web 検索は要求しない．

llama.cpp の OpenAI 互換サーバーへ直接向けるときは，その `/v1` を `base_url` にし，`via_gateway = false` にする．

Karte を使うときは，Karte のデータディレクトリを `karte.data_dir` か環境変数 `KARTE_DATA_DIR` に置く．`karte.mode = "auto"` はディレクトリが無ければ stub になる．`stub` は常に参照なしで答える．

## モック LLM で起動する

Discord へは接続しない．別ターミナルでモックを起動する．

```bash
python -m ephy_discord mock-llm --host 127.0.0.1 --port 8765
python -m ephy_discord answer --config configs/local-mock.toml --text "自己紹介して"
```

`answer` は設定の許可リストを通さず，1問だけローカルモデルへ送る．`check` は Discord にもモデルにも接続せず，トークンも表示しない．

```bash
python -m ephy_discord check --config configs/ephy-discord.example.toml
```

本番の接続は，実 ID を書いた Git 外の toml で行う．

```bash
python -m ephy_discord run --config /path/to/ephy-discord.toml
```

## コマンド

| 入力 | 動作 |
| --- | --- |
| 許可チャンネルでのメンション | ローカルモデルが答える．許可外は無視する． |
| `/ephy ask` | 同じ回答．許可外チャンネルでは実行者にだけ短い拒否を返す． |
| `/ephy forget` | 実行したユーザーの保存データを消す．設定したギルドの中であれば，許可チャンネル以外からでも実行できる． |
| `/ephy purge` | 管理者が全保存データと監査ログを消す． |
| `/ephy stop` | 管理者がプロセスを止める．再起動すると応答を再開する． |

管理者は `discord.admin_user_ids`，またはそのサーバーの Administrator / Manage Server 権限である．

ユーザーあたりの既定は 60 秒に 3 回，チャンネルあたりは 60 秒に 10 回である．超過してもモデルは呼ばない．

## 設定

見本は [configs/ephy-discord.example.toml](configs/ephy-discord.example.toml)．未知のキーは起動時に拒否する．

| キー | 既定 | 意味 |
| --- | --- | --- |
| `discord.guild_id` | 必須 | 霧峰帝国議会のサーバー ID |
| `discord.allowed_channel_ids` | `[]` | 返事を許すチャンネル．空なら失敗として閉じる |
| `discord.admin_user_ids` | `[]` | purge と stop を追加で許すユーザー |
| `llm.base_url` | `http://127.0.0.1:8000/v1` | ループバックの `/v1` |
| `llm.model` | `auto` | Gateway のモデルエイリアス．llama.cpp ではロード済みモデル名 |
| `llm.timeout_seconds` | `120` | チャット完了の待ち時間 |
| `llm.temperature` | `0.7` | |
| `llm.max_tokens` | `800` | |
| `llm.via_gateway` | `true` | true なら `metadata.mode` と `web_search=false` を付ける |
| `llm.gateway_mode` | `fast` | `auto` / `fast` / `work` / `code` / `rag` |
| `karte.mode` | `auto` | `auto` / `filesystem` / `stub` |
| `karte.data_dir` | 空 | 空なら `KARTE_DATA_DIR` |
| `karte.timeout_seconds` | `3` | 1 リクエストの待ち時間．ephy-runtime のクライアントと同じ既定 |
| `karte.projects` | `[]` | 空なら project では絞らない |
| `karte.tags` | `[]` | 指定した tag をすべて持つ文書だけ |
| `karte.sensitivity_ceiling` | `internal` | `public` から `restricted` |
| `karte.top_k` | `5` | 1 から 20 |
| `karte.max_documents` | `3` | read する件数．1 から 3 |
| `karte.actor_id` | `ephy-discord` | actor type は `ephy` 固定 |
| `cache.path` | `data/ephy-discord.sqlite` | |
| `cache.dedupe_seconds` | `600` | 同一質問の再利用 |
| `cache.retention_days` | `7` | 本文と監査の保持 |
| `rate_limit.user_per_minute` | `3` | `window_seconds` のあいだのユーザー上限 |
| `rate_limit.channel_per_minute` | `10` | 同ウィンドウのチャンネル上限 |
| `rate_limit.window_seconds` | `60` | |
| `audit.path` | `data/audit.jsonl` | |
| `discord.approver_user_ids` | `[]` | 承認ボタンを押せるユーザー．空なら提案を受け付けない |
| `mcp.enabled` | `false` | true のときだけ MCP を待つ |
| `mcp.bind_host` | `127.0.0.1` | ループバックだけ |
| `mcp.port` | `8791` | オーケストレータと重なったら変える |
| `mcp.public_base_url` | 空 | トンネルの https URL．Git に入れない |
| `mcp.token_ttl_seconds` | `3600` | 未使用．トークン寿命は外部 IdP が持つ |
| `approval.expiry_seconds` | `900` | 承認ボタンの期限 |

トークンの変数名は `DISCORD_BOT_TOKEN` 固定である．

## 合わせたインターフェース

- チャットは ephy-runtime Gateway の `POST /v1/chat/completions` と同じ OpenAI 互換の JSON である．`model`，`messages`，`temperature`，`max_tokens`，`stream: false`．Gateway 向けには `metadata.mode` と `metadata.web_search: false` を足す．既定 URL は `http://127.0.0.1:8000/v1`．モデルエイリアス `auto` は Gateway の router が受ける．
- Karte は Context Protocol v1．`{KARTE_DATA_DIR}/.mdsys/context/v1/requests/{request_id}.json` へ request を書き，`responses/` の同じ名前を待つ．`protocol_version` は `1.0`．search は `query` と `doc_id: null`，read はその逆．SHA-256 は ephy-runtime の `KarteContextClient` と同じく，sort した indent 2 の JSON に改行を足した生バイトである．Karte はそのファイルバイトを hash する．
- actor は `type: ephy`，`id: ephy-discord`．Karte の policy は id を先に引き，無ければ type `ephy` へ戻る．既定 policy の search / read はこの fallback で通る．id を `ephy` にしたいときは `karte.actor_id` を変える．
- 参照の上限は runtime の grounding と同じく，文書 3 件，1 件 6000 字，合計 12000 字である．read が時間切れのときは search の snippet を使う．

## 公開インターフェースとの差分

- Gateway の `POST /v1/chat/completions` は，`metadata.source_scope` が `personal_context` でない限りローカル RAG を試み，mode の system prompt も足す．O-1 は Karte をボット側で読み，本文を messages に入れる．Gateway 経由では Runtime の RAG が別途付くことがある．参照をボットの Karte 読み取りだけにしたいときは，`via_gateway = false` で llama.cpp の `/v1` へ向ける．
- Karte には Gateway の `POST /v1/karte/context/search` と `/read`，および stdio の MCP facade もある．O-1 は ADR-0008 の v1 transport であるファイルスプールを使う．MCP と Context Protocol v2 は使わない．
- T-098 の ResearchJob は実行可能な契約として受入されていない．O-2 の接続点はまだ無い．

## 開発

```bash
ruff check .
ruff format --check .
pytest
```

GitHub Actions は lint と pytest を実行する．テストは Discord，Gateway，Karte の実体に接続しない．

## 文書

- [PRIVACY.md](PRIVACY.md)
- [Architecture](docs/architecture.md)
- [Repository relationships](docs/repository-relations.md)
- [Security and data handling](docs/security-and-data.md)
- [Roadmap](docs/roadmap.md)

## License

ライセンスは未選定である．
