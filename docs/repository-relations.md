# Repository relationships

`.ephy/project.yaml` が直接の関係を持つ．

- Parent: `ephy`
- Depends on: `ephy-runtime`．回答の OpenAI 互換エンドポイントは Gateway `http://127.0.0.1:8000/v1/chat/completions`，または Gateway が叩く llama.cpp の `/v1` である．
- Integrates with: `karte`．読み取りは Context Protocol v1（`.mdsys/context/v1` の request / response）である．
- O-2 で想定する `ephy-worker` の ResearchJob は，まだこのリポジトリの依存にしていない．契約は未受入の提案である．

`ephy-worker` にある `discord-runtime` は，Runtime の公開検索へ質問を渡す別の transport である．このリポジトリはそのプロセスを起動せず，設定も共有しない．本番では Discord アプリケーションを分ける．
