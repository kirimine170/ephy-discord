param(
 [Parameter(Mandatory=$true)][ValidateSet('Prepare','SetToken','Check','Run','Start','Stop','Status','RegisterLogon','UnregisterLogon')][string]$Action,
 [string]$Manifest,[string]$Name,[string]$CommonPolicy,[string]$Root,[string]$Python,[string[]]$Names
)
$ErrorActionPreference='Stop'
if($env:OS -ne 'Windows_NT'){throw 'WINDOWS_REQUIRED'}
$source=[IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
function WriteUtf8($path,$value){[IO.File]::WriteAllText($path,$value,[Text.UTF8Encoding]::new($false))}
function Quoted($value){if($value.Contains('"') -or $value.Contains("`n")){throw 'INVALID_ARGUMENT'};return '"'+$value+'"'}
function CheckName($value){if($value -cnotmatch '^[A-Za-z][A-Za-z0-9_-]{0,63}$'){throw 'INVALID_REGISTERED_NAME'}}
function TomlString($value){return ($value.Replace('\','/') | ConvertTo-Json -Compress)}
function CurrentSource {
 $sha=(& git -C $source rev-parse HEAD).Trim()
 if($LASTEXITCODE -ne 0 -or $sha -notmatch '^[a-f0-9]{40}$'){throw 'SOURCE_IDENTITY_UNAVAILABLE'}
 $dirty=(& git -c core.fsmonitor=false -C $source status --porcelain)
 if($LASTEXITCODE -ne 0){throw 'SOURCE_IDENTITY_UNAVAILABLE'}
 return @{commit=$sha;clean= -not [bool]$dirty}
}
if($Action -eq 'Prepare'){
 if(-not $Root -or -not $CommonPolicy -or -not $Python){throw 'PREPARE_ARGUMENTS_REQUIRED'}
 $rootPath=[IO.Path]::GetFullPath($Root)
 if(Test-Path -LiteralPath $rootPath){throw 'NEW_PRIVATE_ROOT_REQUIRED'}
 $common=Get-Content -LiteralPath $CommonPolicy -Raw | ConvertFrom-Json
 if(-not $Names){$Names=@($common.self_bot)}
 New-Item -ItemType Directory -Path $rootPath | Out-Null
 $profiles=@()
 foreach($alias in $Names){
  CheckName $alias
  if(-not ($common.bots.PSObject.Properties.Name -ccontains $alias)){throw 'UNREGISTERED_RECIPIENT'}
  $private=Join-Path $rootPath $alias
  New-Item -ItemType Directory -Path $private | Out-Null
  $policy=$common | ConvertTo-Json -Depth 30 | ConvertFrom-Json
  $policy.self_bot=$alias;$policy.state_dir=Join-Path $private 'state'
  $policyFile=Join-Path $private 'policy.json'
  WriteUtf8 $policyFile ($policy | ConvertTo-Json -Depth 30)
  $config=Join-Path $private 'app.local.toml'
  $body=@"
[discord]
guild_id = $($common.guild_id)
allowed_channel_ids = [$($common.channel_id)]
[llm]
base_url = "http://127.0.0.1:9/v1"
model = "unused-by-parliament"
via_gateway = false
[karte]
mode = "stub"
[cache]
path = $(TomlString (Join-Path $private 'adapter.sqlite'))
store_all_messages = false
[audit]
path = $(TomlString (Join-Path $private 'audit.jsonl'))
[mcp]
enabled = false
[parliament]
policy_file = $(TomlString $policyFile)
read_message_content = false
"@
  WriteUtf8 $config $body
  $env:PYTHONPATH=Join-Path $source 'src'
  & $Python -m ephy_discord check --config $config | Out-Null
  if($LASTEXITCODE -ne 0){throw 'POLICY_VALIDATION_FAILED'}
  $profiles+=@{name=$alias;config=$config;policy=$policyFile;private=$private}
 }
 $identity=CurrentSource
 $record=@{version=1;source=$source;python=[IO.Path]::GetFullPath($Python);source_commit=$identity.commit;source_clean=$identity.clean;profiles=$profiles}
 WriteUtf8 (Join-Path $rootPath 'manifest.json') ($record | ConvertTo-Json -Depth 10)
 Write-Output "Prepared=True; Profiles=$($profiles.Count); SourceClean=$($identity.clean); Started=False"
 return
}
if(-not $Manifest -or -not $Name){throw 'MANIFEST_AND_NAME_REQUIRED'}
CheckName $Name
$record=Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
if($record.version -ne 1 -or $record.source -cne $source){throw 'SOURCE_BINDING_MISMATCH'}
$entry=@($record.profiles | Where-Object {$_.name -ceq $Name})
if($entry.Count -ne 1){throw 'RECIPIENT_NOT_PREPARED'}
$entry=$entry[0]
$policy=Get-Content -LiteralPath $entry.policy -Raw | ConvertFrom-Json
$bot=$policy.bots.PSObject.Properties[$Name].Value
$secretPath=Join-Path $entry.private 'bot-token.dpapi'
$pidPath=Join-Path $entry.private 'process.json'
$taskName='Kirimine Parliament '+$Name
function Token {
 $secure=Import-Clixml -LiteralPath $secretPath
 if($secure -isnot [Security.SecureString]){throw 'DPAPI_TOKEN_INVALID'}
 return [Net.NetworkCredential]::new('', $secure).Password
}
function GetApi($path,$token){
 try{return Invoke-RestMethod -Method Get -Uri ('https://discord.com/api/v10/'+$path) -Headers @{Authorization=('Bot '+$token);'User-Agent'='DiscordBot (https://github.com/kirimine170/ephy-discord, 0.1.0)'} -TimeoutSec 10}
 catch{throw 'DISCORD_READ_FAILED'}
}
function OwnedProcess {
 if(-not (Test-Path -LiteralPath $pidPath)){return $null}
 $saved=Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
 $p=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$saved.pid) -ErrorAction SilentlyContinue
 if($p -and $p.CreationDate.ToUniversalTime().ToString('o') -ceq $saved.started -and $p.CommandLine.Contains($PSCommandPath) -and $p.CommandLine.Contains($Manifest)) {return $p}
 return $null
}
function OwnedTask {
 $task=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
 if(-not $task){return $null}
 $expected='-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File '+(Quoted $PSCommandPath)+' -Action Run -Manifest '+(Quoted $Manifest)+' -Name '+$Name
 $exe=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
 if($task.Actions.Count -ne 1 -or $task.Actions[0].Execute -ine $exe -or $task.Actions[0].Arguments -cne $expected){throw 'TASK_OWNERSHIP_MISMATCH'}
 $current=[Security.Principal.WindowsIdentity]::GetCurrent()
 $owner=$task.Principal.UserId
 $sid=if($owner -match '^S-1-'){$owner}else{([Security.Principal.NTAccount]::new($owner)).Translate([Security.Principal.SecurityIdentifier]).Value}
 if($sid -cne $current.User.Value -or [string]$task.Principal.RunLevel -ne 'Limited'){throw 'TASK_PRINCIPAL_MISMATCH'}
 return $task
}
switch($Action){
 'SetToken' {
  $secure=Read-Host ('Token for '+$Name+' (hidden)') -AsSecureString
  $token=[Net.NetworkCredential]::new('', $secure).Password
  try{
   $user=GetApi 'users/@me' $token;$app=GetApi 'oauth2/applications/@me' $token
   if(-not $user.bot -or [string]$user.id -cne [string]$bot.user_id -or [string]$app.id -cne [string]$bot.application_id){throw 'AUTHENTICATED_BOT_MISMATCH'}
   $secure | Export-Clixml -LiteralPath $secretPath
   Write-Output 'Stored=True; IdentityMatched=True; TokenDisplayed=False'
  }finally{$token=$null;$secure.Dispose()}
 }
 'Check' {
  $token=Token
  try{
   $user=GetApi 'users/@me' $token;$app=GetApi 'oauth2/applications/@me' $token
   $member=GetApi ('guilds/'+$policy.guild_id+'/members/'+$bot.user_id) $token
   $roles=GetApi ('guilds/'+$policy.guild_id+'/roles') $token
   $channel=GetApi ('channels/'+$policy.channel_id) $token
   [ordered]@{IdentityMatched=([string]$user.id -ceq [string]$bot.user_id -and $user.bot -and [string]$app.id -ceq [string]$bot.application_id);GuildMemberMatched=([string]$member.user.id -ceq [string]$bot.user_id);DeveloperRoleExists=(@($roles | Where-Object {[string]$_.id -ceq [string]$policy.developer_role_id}).Count -eq 1);ChannelMatched=([string]$channel.guild_id -ceq [string]$policy.guild_id -and [string]$channel.id -ceq [string]$policy.channel_id);ModelCalled=$false;MessageSent=$false}|ConvertTo-Json -Compress
  }finally{$token=$null}
 }
 'Run' {
  $identity=CurrentSource
  if(-not $record.source_clean -or -not $identity.clean -or $identity.commit -cne $record.source_commit){throw 'REVIEWED_SOURCE_CHANGED'}
  if($policy.enabled -ne $true){throw 'POLICY_DISABLED'}
  $old=OwnedProcess
  if($old -and $old.ProcessId -ne $PID){throw 'ALREADY_RUNNING'}
  $current=Get-CimInstance Win32_Process -Filter ('ProcessId='+$PID)
  WriteUtf8 $pidPath (@{pid=$PID;started=$current.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json)
  $env:PYTHONPATH=Join-Path $source 'src'
  $env:DISCORD_BOT_TOKEN=Token
  try{& $record.python -m ephy_discord run --config $entry.config --require-parliament;exit $LASTEXITCODE}
  finally{Remove-Item Env:DISCORD_BOT_TOKEN -ErrorAction SilentlyContinue}
 }
 'Start' {
  if(OwnedProcess){throw 'ALREADY_RUNNING'}
  $args=@('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',(Quoted $PSCommandPath),'-Action','Run','-Manifest',(Quoted $Manifest),'-Name',$Name)
  $p=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe') -ArgumentList $args -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $entry.private 'stdout.log') -RedirectStandardError (Join-Path $entry.private 'stderr.log')
  $observed=Get-CimInstance Win32_Process -Filter ('ProcessId='+$p.Id)
  WriteUtf8 $pidPath (@{pid=$p.Id;started=$observed.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json)
  Write-Output 'Started=True; Hidden=True'
 }
 'Stop' {
  $p=OwnedProcess
  if($p){& (Join-Path $env:SystemRoot 'System32/taskkill.exe') /PID $p.ProcessId /T /F | Out-Null;if($LASTEXITCODE -ne 0){throw 'STOP_FAILED'}}
  if(OwnedTask){Stop-ScheduledTask -TaskName $taskName}
  Write-Output 'Stopped=True'
 }
 'Status' {Write-Output ('OwnedProcessRunning='+[bool](OwnedProcess))}
 'RegisterLogon' {
  if(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue){throw 'TASK_ALREADY_EXISTS'}
  $identity=CurrentSource
  if(-not $identity.clean -or -not $record.source_clean -or $identity.commit -cne $record.source_commit){throw 'REVIEWED_SOURCE_CHANGED'}
  $exe=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
  $args='-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File '+(Quoted $PSCommandPath)+' -Action Run -Manifest '+(Quoted $Manifest)+' -Name '+$Name
  $action=New-ScheduledTaskAction -Execute $exe -Argument $args
  $trigger=New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
  $principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
  $settings=New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
  Write-Output 'LogonTaskRegistered=True; Started=False'
 }
 'UnregisterLogon' {if(OwnedTask){Unregister-ScheduledTask -TaskName $taskName -Confirm:$false};Write-Output 'LogonTaskRemoved=True'}
}
