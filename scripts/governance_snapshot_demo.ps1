param([ValidateSet('Start','Stop')][string]$Action='Start')
$ErrorActionPreference='Stop'
$demoRoot=Split-Path -Parent $PSScriptRoot
$demoScript=Join-Path $PSScriptRoot 'governance_snapshot_server.mjs'
$demoNode=(Get-Command node -ErrorAction Stop).Source
# Host, port and directory come from the shared demo configuration, environment overrides included.
$demoConfig=& $demoNode $demoScript --print-config | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $demoConfig) { throw 'Demo configuration rejected.' }
$demoDirectory=$demoConfig.directory
$demoPidFile=Join-Path $demoDirectory 'server.pid.json'

if ($Action -eq 'Stop') {
    if (-not (Test-Path -LiteralPath $demoPidFile)) { Write-Output 'No recorded demo process.'; return }
    $demoRecord=Get-Content -LiteralPath $demoPidFile -Raw | ConvertFrom-Json
    $demoProcess=Get-Process -Id $demoRecord.pid -ErrorAction SilentlyContinue
    if (-not $demoProcess) { Write-Output 'The recorded demo process is already stopped.'; return }
    $demoCommand=Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $demoRecord.pid)
    if ($demoProcess.StartTime.ToUniversalTime().Ticks.ToString() -ne $demoRecord.start_ticks -or
        -not $demoCommand.CommandLine.Contains($demoScript) -or
        -not $demoCommand.CommandLine.Contains('--demo-fixtures-only')) {
        throw 'PID identity mismatch; no process was stopped.'
    }
    Stop-Process -Id $demoRecord.pid -ErrorAction Stop
    Write-Output 'Only the recorded local fixture server was stopped; logs preserved.'
    return
}

if ($env:NODE_ENV -eq 'production') { throw 'Demo is disabled in production.' }
if (Test-Path -LiteralPath $demoPidFile) {
    $previousDemo=Get-Content -LiteralPath $demoPidFile -Raw | ConvertFrom-Json
    $previousProcess=Get-Process -Id $previousDemo.pid -ErrorAction SilentlyContinue
    if ($previousProcess -and $previousProcess.StartTime.ToUniversalTime().Ticks.ToString() -eq $previousDemo.start_ticks) {
        throw 'A recorded demo is still running; inspect or stop it before starting another.'
    }
}
New-Item -ItemType Directory -Force -Path $demoDirectory | Out-Null
$demoStamp=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
$demoLog=Join-Path $demoDirectory ('server-'+$demoStamp+'.jsonl')
$demoErrorLog=Join-Path $demoDirectory ('server-'+$demoStamp+'.stderr.log')
$demoProcess=Start-Process -FilePath $demoNode -ArgumentList @(('"{0}"' -f $demoScript),'--demo-fixtures-only') -WorkingDirectory $demoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $demoLog -RedirectStandardError $demoErrorLog
Start-Sleep -Milliseconds 500
if ($demoProcess.HasExited) { throw 'Demo failed to start. Inspect its local sanitized stderr log.' }
@{pid=$demoProcess.Id;start_ticks=$demoProcess.StartTime.ToUniversalTime().Ticks.ToString();stdout=$demoLog;stderr=$demoErrorLog;classification='sanitized_demo_fixture';host=$demoConfig.host;port=$demoConfig.port} | ConvertTo-Json | Set-Content -LiteralPath $demoPidFile -Encoding UTF8
Write-Output ('Fixture-only snapshot server: {0}' -f $demoConfig.url)
Write-Output 'No user authentication, commercial runtime or live events. Use -Action Stop to stop this exact process.'
