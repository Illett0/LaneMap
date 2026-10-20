# LaneMap 入力補助エディタを止める（editor.pid の PID を止める）
$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'editor.pid'

if (-not (Test-Path $pidFile)) {
    Write-Host "エディタは起動していません（editor.pid がありません）。"
    exit 0
}
$editorPid = Get-Content $pidFile | Select-Object -First 1
$proc = Get-Process -Id $editorPid -ErrorAction SilentlyContinue
if ($proc -and $proc.ProcessName -like 'python*') {
    Stop-Process -Id $editorPid -Force
    Write-Host "エディタを止めました（PID $editorPid）。"
} else {
    Write-Host "PID $editorPid のエディタは動いていませんでした。"
}
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
