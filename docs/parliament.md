# Discord議会の登録IDとdeveloper依頼

この機能は既定で無効です．通常の`ephy-discord run --config ...`に接続した任意moduleであり，既存の事務局，slash command，Main GrokのRoutine，OAuth，購読，owner repair，main branchを変更しません．登録数は2〜32です．Grok代表，Dot，Codexの3体でも，既存の代表だけをGrok入口とする構成でも利用できます．登録名はlocal aliasであり，権限は実際のDiscord user／application IDと最新member情報から判定します．研究結果に書かれたHackSick，ESSEN等の名前，引用，Webhook表示名，共有Main資格は別の実行主体になりません．

## 設定する範囲

`configs/parliament-policy.example.json`は架空IDだけの例です．Git外のprivate directoryへコピーし，対象guild／親channel／既存developer roleの正確なID，登録Bot user／application ID，明示的なrepository aliasを設定します．policyとstateはcode／対象repositoryの外へ置きます．`repositories`はpath，依頼可能member ID，宛先Bot alias，変更可能prefix，GitHub repository，base branch，固定check argvと実行fileのSHAを持ちます．質問文からpath／command／remote／roleを追加しません．

既存代表を登録するだけなら，そのtokenをこのruntimeへ渡す必要はありません．実際にこのGatewayとして動かす宛先だけを選びます．登録Botは，実装依頼の際に本人のmemberがdeveloper roleを持ち，repository allowlistにも含まれる必要があります．このソフトは招待，role付与，新しいcredential登録を自動で行いません．

通常AppConfigへ追加するのは次の設定だけです．

```toml
[parliament]
policy_file = 'C:/private/parliament/Builder/policy.json'
read_message_content = false
```

`read_message_content=false`は新しいMessage Content権限を要求しません．直接mentionを含む依頼を使ってください．pingを伴わない通常replyも読む運用では，既存のMessage Content許可を確認してから明示的にtrueにします．Portalの許可は自動変更しません．人間は同じchannel／public threadのこのBotの返信へ質問できます．読めないcontentを推測して受け付けません．

## 依頼形式

先頭行が宛先の実user IDと一致する次の形式だけを処理します．本文は2行目以降です．引用内のcommandは依頼になりません．

```text
<@BOT_USER_ID> ask
質問

<@BOT_USER_ID> opinion notify=<@HUMAN_MEMBER_ID>
意見を求める内容

<@BOT_USER_ID> implement example
対象repositoryに必要な変更
```

登録済みBotからは，先頭行へ`request=安定した相関ID hop=0`も指定します．BotによるBot返信への再委譲は拒否します．相関IDとmessage IDの再配送を永続的に抑止し，Botごとに120秒の受付間隔と同時未確定1件，runtimeごとに実行1件／待機128件を上限とします．生成返信は`[parliament-result v1 ...]`で始まり，新しい依頼にはなりません．人への通知は明示した実memberだけで，role／everyoneは通知しません．

通常の事務局のslash commandや別Botへのmentionに反応せず，新しいslash登録／共有MCP portも作りません．処理対象channelの全会話をgeneric履歴や検索へ送らず，明示依頼だけをprivate ledgerへ保存します．

## 認可と結果配送

SDKで認証した現在userとapplicationを登録IDに照合してから受信を開始します．受付，実行直前，公開直前にDiscord RESTのguild，channel，member，roleを取得します．cached roleや表示名は使いません．実装にはdeveloper roleとrepository allowlistの両方が必要です．受付policyのdigestはjobに固定し，policy変更後の再起動で古い依頼を新しいrepositoryへ読み替えません．

結果は保存したguild／channel／thread／requesterへだけ返信します．原messageと現在memberを再照合し，未知の実行結果，送信開始後の切断，公開結果不明は自動再実行・再送しません．`execution=running/unknown`，`delivery=sending`とprivateの`publication.json`は運用者が確認する状態です．履歴を削除して再試行せず，既存のcommit／branch／PRを読み取り確認してください．

## CodexからDraft PRまで

実装依頼は登録先の名前と無関係に共通Codex実装serviceへ渡します．認可主体は元の実Discord memberのままです．`backend=codex`の質問／意見は空のGit scratchをread-onlyで使い，repositoryを暗黙に読みません．Grok／Dotの会話には[既存run／turnアダプタ](../adapters/orchestrator/README.md)を実装しています．固定loopback `/v1/parliament`へ1回だけ開始要求を送り，以後100秒までGETで確認します．version／job／guild／channel／requester／recipient／kind／messageとrun／turnを完全照合します．IDは10進文字列です．既存本人の認証済みcompleted回答と元threadへの配送が確認されたらURLだけを返し，本文を二重投稿しません．未設定・不明なら成功にせず，別モデルやpersonaへfallbackしません．Grok代表は既存Mainの接続を保ち，専門Botはその内部委任です．

`codex.enabled=false`の代わりに，以下のoperator設定を用意します．値は既存環境から確認し，新しいlogin／tokenは作りません．

```json
{
  "enabled": true,
  "binary": "C:/existing/codex.exe",
  "binary_sha256": "<actual file SHA256>",
  "git": "C:/existing/git.exe",
  "git_sha256": "<actual file SHA256>",
  "home": "C:/existing-Codex-home",
  "read_roots": ["C:/existing-Python-runtime", "C:/existing-venv", "C:/existing-Git-runtime", "C:/existing-Codex-bin"],
  "denied_roots": ["C:/private-unrelated-data"],
  "publish_draft_pr": true
}
```

既存Codex loginはtrusted CLIが使い，agent toolからCODEX_HOMEは読めません．個人のconfig，apps，plugins，web searchを継承せず，GitHub資格はworkerへ渡しません．公式MXCを明示し，非対応時はsandboxを緩めず停止します．手書きの証明fileや未実装外部launcherは不要です．各jobで無害なcanaryを使い，親子両方の禁止読書き，外部network拒否，candidateのread／writeまたはread-onlyを実測します．allow readが保護rootを再び開く設定は拒否します．

```powershell
python -m ephy_discord parliament-sandbox-check --codex C:/existing/codex.exe --root C:/private/new-canary-run
```

このcheckはモデルやDiscord，GitHubを呼びません．新しいrootを指定し，生成された測定記録を保持します．公式説明は[permission profiles](https://learn.chatgpt.com/docs/permissions)と[Windows MXC](https://learn.chatgpt.com/docs/windows/windows-sandbox)を参照してください．

sourceはcleanなprimary Git checkoutを使います．Gitのinclude，fsmonitor，filter等の実行可能設定は隔離前に拒否し，global／system Git設定とhookをworker準備へ持ち込みません．モデルはbase SHAから作った専用worktreeだけを編集します．既存test，conftest，AGENTS，隠し設定，scope外，case-only renameは公開しません．

モデル終了後に別のread-only snapshotを凍結し，固定checkをMXCで実行します．Pythonの`-c`，`-m pytest`，`-m ruff`とNodeの固定`-e`checkを扱います．Nodeの非同期checkerは明示的にawaitしてください．checkが戻った後のnonce付き正常完了記録も必要なので，候補によるearly `exit 0`だけでは公開しません．これはcheckの正常復帰を観測する仕組みであり，testの意味や同一interpreter内の悪意あるintrospectionを暗号学的に証明するものではありません．Draft PRのCIとreviewは引き続き必要です．

publisherだけが既存Git credential helperを使います．固定repository／remote baseを照合し，専用branchへ検証済みfileだけを反映します．再構成worktreeとcommitの再checkoutをmanifestへ照合し，remote branchが存在しないことを空のleaseで原子的に要求します．Draft PRのURLが正規依頼元へ戻ります．mainへのcommit，merge，release，deploy，repository権限変更はしません．公開途中の不明結果も再送しません．

## Windows運用補助

共通のPython環境を1つ指定し，`windows/parliament.ps1`で選択した宛先だけの設定を生成できます．以下は手順であり，この機能の追加によって実行されません．real IDをpublic repositoryへ保存しないでください．

```powershell
# Git外でIDとrole，repository許可を確定した共通policyを入力する．
./windows/parliament.ps1 -Action Prepare -CommonPolicy C:/private/common-policy.json -Root C:/private/new-parliament -Python C:/existing-venv/Scripts/python.exe -Names Builder
# tokenは本人が1050機のconsoleで非表示入力する．平文をcommand引数へ書かない．
./windows/parliament.ps1 -Action SetToken -Manifest C:/private/new-parliament/manifest.json -Name Builder
./windows/parliament.ps1 -Action Check -Manifest C:/private/new-parliament/manifest.json -Name Builder
# 後続の明示的な配備承認後だけ起動する．
./windows/parliament.ps1 -Action Start -Manifest C:/private/new-parliament/manifest.json -Name Builder
./windows/parliament.ps1 -Action Status -Manifest C:/private/new-parliament/manifest.json -Name Builder
# OSログオン時の再開が承認された場合だけ，同じuserのlimited taskを登録する．
./windows/parliament.ps1 -Action RegisterLogon -Manifest C:/private/new-parliament/manifest.json -Name Builder
```

SetTokenは登録user／applicationを読み取り照合して，同じWindows userだけが復号できるDPAPI fileへ保存します．Checkは所属，channel，既存roleの存在をGETで確認し，投稿や付与を行いません．Run／Startはreviewしたclean source commitが変わっていれば停止します．Stop／UnregisterLogonはこの補助が所有するprocess／taskだけが対象です．従来事務局のprocess，task，購読は操作しません．未招待Botや未確定roleを確認済みとして扱わず，実際の招待・権限追加・配備は別の人間操作です．
