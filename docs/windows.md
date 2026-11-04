# Windows（GTX 1050 のノート）

霧峰帝国議会のオーケストレータと同じ PC で動かすときの手順である．Linux の起動は README の `python -m ephy_discord run` のままである．

## 準備

1. Python 3.12 を入れ，`python` が PATH で見えるようにする．
2. リポジトリで `python -m venv .venv` と `pip install -e .` を実行する．起動スクリプトは PATH 上の `python` を使うので，その venv を有効にしたシェルから起動するか，venv の `python.exe` を PATH の先頭に置く．
3. `configs/ephy-discord.example.toml` を Git に入らない `configs/ephy-discord.local.toml` へコピーし，霧峰帝国議会のサーバー ID，許可チャンネル，承認者 ID を書く．MCP を使うときだけ `enabled = true` にする．
4. 秘密情報は引数に渡さない．PowerShell で次を実行し，プロンプトへ入力する．

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File windows\protect-secret.ps1 -Name DISCORD_BOT_TOKEN
powershell -NoProfile -ExecutionPolicy Bypass -File windows\protect-secret.ps1 -Name EPHY_MCP_BEARER_TOKEN
```

DPAPI（CurrentUser）の暗号文は `%LOCALAPPDATA%\ephy-discord\secrets.dpapi` の1ファイルである．`setx` やユーザー環境変数，`.env` は作らない．起動スクリプトは子プロセスへ渡したあと，親の環境変数を元に戻す．他の Windows ユーザーでは解けない．Git には入れない．

## 起動，停止，状態

```bat
windows\start.cmd
windows\status.cmd
windows\stop.cmd
```

`start.ps1` は DPAPI の中身をそのプロセスの環境変数へ戻してから `python -m ephy_discord run` を起動する．値は表示しない．設定ファイルの場所を変えるときは `EPHY_DISCORD_CONFIG` を絶対パスで置く．

ログオン時に起動するタスクは次で登録し，外すときは削除する．

```bat
windows\install-logon-task.cmd
windows\uninstall-logon-task.cmd
```

タスク名は `ephy-discord` である．オーケストレータのタスク名とは別にしている．

## MCP と Cloudflare

MCP は `127.0.0.1:8791` で待つ．同じ PC のオーケストレータは `127.0.0.1:18640` である．ephy のポートを 18640 にすると起動スクリプトは拒否する．LAN へは直接開けない．`status` は記録したプロセス開始識別子と実行ファイルが一致するときだけ稼働中と出す．PID の再利用では止めない．

コネクタから届けるときは，公開することを確認してから別プロセスで quick tunnel を張る．

```bat
cloudflared tunnel --url http://127.0.0.1:8791 --http-host-header 127.0.0.1:8791
```

表示された `https://….trycloudflare.com` を，生ログを残さずに記録する．

```bat
python -m ephy_discord mcp-url --config configs\ephy-discord.local.toml set https://NEW.trycloudflare.com
```

ホスト名は再起動で変わる．変わったら記録とコネクタ登録を人が直す．名前付きトンネルにするとホスト名を固定できる．トンネルのトークンも DPAPI に置き，リポジトリへは入れない．

コネクタの登録で人が行うこと：

1. Discord Developer Portal で，このボット用のアプリケーションを作る．オーケストレータの Bot token は使い回さない．
2. Message Content Intent だけを有効にし，README の権限 `2147552256` で霧峰帝国議会へ招待する．
3. ChatGPT，Grok，Muse のカスタムコネクタに，トンネルの `https` URL の `/mcp` を登録する．OAuth を選ぶか，Bearer に `EPHY_MCP_BEARER_TOKEN` を貼る．
4. 呼び出しは各チャット画面から人が行う．ephy が相手の API を購読の代わりに叩く設定にはしない．
