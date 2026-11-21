# 既存議会サービスへの会話アダプタ

このディレクトリは，Grok／Dotの明示的な入力を既存サービスの通常run／turnへ接続する実装です．`parliament.ts`がnative runtime内の受け口です．別のAIを起動したり，独立processから本番DBを開いたりしません．専門Botは既存のGrok代表から扱い，このアダプタへ専門Botの新しい接続先や資格を登録しません．

既存Grokの`routine`接続と既存Dotの`events`接続を使います．現在のサービス内部では後者のconnection IDは`mia`です．**Dotという表示名からmiaへ自動対応させません**．担当者が実際のDiscord user IDと既存のOAuth bindingの対応を確認し，そのbindingのSHA256をprivate設定へ固定した場合だけ使えます．Grok代表の対応も同じ条件です．shared Main資格を別personaの本人証明として扱いません．

## 実装された経路

Gatewayは`POST /v1/parliament`を1回だけ送り，以後は`GET ?job_id=...`だけで結果を読みます．Discord snowflakeは10進文字列で渡し，JavaScriptの整数丸めを避けます．実装依頼はこの受け口では拒否し，既存のdeveloper role／repository認可付きCodex経路を使います．

受け口は，既存公開ポートとは異なる明示設定のIPv4 loopback listenerです．同じnative process内で動き，既存公開HTTP listenerにはrouteを追加しません．転送・proxy・Origin等のheaders，異なるHostや非loopback socketは拒否します．public tunnelへprivate portを公開しません．

既存サービスのBot REST資格で，実guild／channel／member／依頼messageをDiscordから読み取り照合します．本文・宛先・実authorが一致する120秒以内の明示依頼だけが開始できます．GatewayからOAuth tokenやBot tokenを渡しません．loopbackという理由だけで申告authorを信頼せず，本文が読めない場合も停止します．Humanの通常replyは宛先Botの実返信を参照している場合に限り扱います．Webhook，引用command，別Bot宛，添付，everyone，未登録Bot，BotからBotの返信への連鎖は拒否します．

選択した既存connection・現在のnative binding・既存project/member grantを確認して`Orchestrator.create`を呼び，現在の実authorをそのままrun ownerへ保存します．人間ownerやMia principalへの置き換えはしません．1turn・画像なし・120秒を上限とし，`parent_message_id`と専用台帳の配送先は元message／元channelに固定します．既存`runs.thread_id`の一意制約は変更せず，この経路ではnullを保ちます．既存Outboxが専用台帳の確定配送先を使うため，同じchannel／threadで複数依頼や返信を扱えて，新しい親投稿や別threadは作りません．

通常のrunner→Grok Routine／Dot Events→本人のOAuth `get_turn`／`submit_turn`→既存Outboxが回答を配送します．policy，binding，transport，grantは毎回照合します．受付・初回dispatch・実投稿・Events配送の直前にはmessage／memberをRESTで再確認します．待機中の同時観測はまとめ，成功した観測を最長3秒だけ共有します．429・一時5xx・通信待ちの失敗はbackoff後にGETだけで再観測し，それだけでrunを恒久失敗や再dispatchにしません．既存のcallback challenge・generation・subscription検証は維持します．研究依頼のAPIやmiaのturn回答を装った入力は使いません．

回答本文は既存Outboxが元threadへ送ります．Gatewayが返すのは，認証済みcompleted submissionと`POSTED`・`SENT`・実message IDを照合した配送URLだけです．回答を別モデルで再構成したり，同じ本文を二重投稿したりしません．その実回答への人間の通常replyも，SENT outbox・署名済みsubmissionのhash・現在の宛先binding・実messageのsender／relay markerを再照合して同じ宛先へ渡せます．共有事務局のsender名から宛先を推定しません．別Botへの明示mentionは通常replyのfallbackにしません．受付・通知2xx・モデルの自己申告だけでは成功にしません．同一message／宛先・Bot相関IDは永続重複排除し，不明な開始や配信を再発火しません．

## 反映前の候補を作る

`prepare.mjs`はレビューした既存sourceのSHAを照合して，新しいディレクトリへsource候補を作ります．稼働中source，data，config，資格，processは変更しません．7つの既存fileへ任意のhookを加え，`src/native/parliament.ts`を配置します．既存依存を読み取り専用で利用して候補serverと合成testをbuildします．npm installやnode_modulesへのlinkは作りません．出力bundleには検証機の依存pathが含まれるため，そのbinaryを本番へ配布しません．反映担当は候補sourceをレビューし，対象環境の通常buildを使います．

```powershell
node adapters/orchestrator/prepare.mjs --source C:/existing-orchestrator-source --destination C:/private/new-adapter-candidate
cd C:/private/new-adapter-candidate
node --test --test-reporter=spec parliament-tests.mjs
node C:/existing-orchestrator-source/node_modules/typescript/bin/tsc --noEmit --project tsconfig.integration.json
```

source SHAが違う場合はpatchを当てず停止します．`integration-manifest.json`の変更前後SHAを担当者へ渡してください．本番への自動copy，設定有効化，再起動，既存jobの再実行はこのscriptにはありません．

## 担当側で確定する最小設定

次は架空IDの構造例です．本番値をGitへ保存しません．`identity_sha256`は現在の既存認証bindingを担当者が照合する値であり，新しい認証情報ではありません．確認できなければ有効にしません．

```json
{
  "parliament": {
    "version": 1,
    "enabled": true,
    "port": 18641,
    "guild_id": "1",
    "channel_id": "10",
    "project_id": "parliament-test",
    "registered_bot_ids": ["100", "200", "300"],
    "recipients": {
      "100": {"connection_id": "grok", "identity_sha256": "<verified existing binding SHA256>"},
      "200": {"connection_id": "mia", "identity_sha256": "<verified existing binding SHA256>"}
    }
  }
}
```

既定は未設定または`{"version":1,"enabled":false}`で無効です．保存済みbridge台帳があれば，無効化・設定削除後の再起動でもguardだけは残して旧jobを通常議論へ読み替えません．private listenerは有効時だけ開きます．通常MCPの公開toolや既存情報請求flagは増減しません．例のportは実際に競合しないprivate portへ担当者が確定します．Gateway private policyのGrok／Dotの`conversation_url`だけをその同じloopback endpointへ向け，Codexは`backend=codex`にします．既存serviceの`ALLOWED_USER_IDS`とproject membershipにない依頼者を，既存ownerの資格へ読み替えません．必要なgrantは担当側で明示的に確認します．

本人の操作は次に限定します．紐づけ・招待・資格・権限・本番切り替えは主担当側が一度だけ扱います．1050側から並行実施しません．

1. 3体の実user／application IDと既存Grok／Dot本人の対応を確認する．専門Botの新規登録は不要．
2. 主担当が案内する既存Botの招待と対象channelでの閲覧・返信・public thread送信範囲を確認する．既存事務局のBot RESTで明示依頼本文が読めなければ停止し，必要な許可を確認してから判断する．
3. Codex実装を許可する実memberのdeveloper role IDとrepositoryごとの許可を確定する．owner修復の包括許可を流用しない．
4. tokenが必要な宛先だけ，担当が指定した非表示入力を使う．チャットやcommand引数へ秘密値を渡さない．
5. 本番反映後，担当側が新しい公開情報だけの小規模依頼で実往復を確認する．合成テストを本番接続の証明にしない．

GitHub公開は既存のChatGPT GitHub接続を使い，1050で新しいGitログインやtoken登録を求めません．今回のPR公開権限は主担当が対象repositoryだけへ追加した範囲を使います．Botからの将来の自動PR公開についても，承認済みpublisherが利用できない場合は停止し，権限を自動で拡張しません．
