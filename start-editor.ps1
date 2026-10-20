# LaneMap 入力補助エディタを起動する（すでに起動していればブラウザを開くだけ）
#   .\start-editor.ps1                       # horikawa-lanes.csv を編集
#   .\start-editor.ps1 -Csv other.csv -Port 8770
param(
    [string]$Csv = "horikawa-lanes.csv",
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$pidFile = Join-Path $root 'editor.pid'
$logDir = Join-Path $root 'logs'
$url = "http://127.0.0.1:$Port/"

if (Test-Path $pidFile) {
    $old = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Host "エディタはすでに起動しています（PID $old）。ブラウザを開きます: $url"
        Start-Process $url
        exit 0
    }
    Remove-Item $pidFile -Force   # 前回の残り
}

New-Item -ItemType Directory -Force $logDir | Out-Null
$python = (Get-Command python -ErrorAction Stop).Source
$pyArgs = @('-X', 'utf8', ('"{0}"' -f (Join-Path $root 'editor.py')), ('"{0}"' -f $Csv), '--port', $Port, '--no-browser')
Start-Process -FilePath $python -ArgumentList $pyArgs -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'editor.out.log') `
    -RedirectStandardError (Join-Path $logDir 'editor.err.log') | Out-Null

# サーバーが応答するまで待つ（最大 10 秒）
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    try {
        Invoke-WebRequest -Uri "${url}api/data" -UseBasicParsing -TimeoutSec 2 | Out-Null
        $editorPid = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        Write-Host "エディタを起動しました（PID $editorPid）: $url"
        Start-Process $url
        exit 0
    } catch { }
}
Write-Host "起動を確認できませんでした。logs\editor.err.log を見てください:" -ForegroundColor Red
Get-Content (Join-Path $logDir 'editor.err.log') -ErrorAction SilentlyContinue | Select-Object -Last 20
exit 1
