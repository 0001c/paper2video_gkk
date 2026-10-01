param([int]$Port = 8848)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskNetwork = Get-NetIPConfiguration | Where-Object {
    $_.IPv4DefaultGateway -and $_.NetAdapter.HardwareInterface -and $_.IPv4Address
} | Select-Object -First 1
if (-not $taskNetwork) { throw '未找到有默认网关的物理局域网网卡。' }
$taskAddress = $taskNetwork.IPv4Address | Select-Object -First 1
$taskSubnet = "$($taskAddress.IPAddress)/$($taskAddress.PrefixLength)"
Write-Host "局域网访问地址：http://$($taskAddress.IPAddress):$Port"
Push-Location -LiteralPath $taskRoot
try {
    & "$taskRoot/.venv/Scripts/python.exe" web/app.py --host 0.0.0.0 --port $Port --lan-subnet $taskSubnet
    if ($LASTEXITCODE -ne 0) { throw "服务启动失败，退出码：$LASTEXITCODE" }
} finally { Pop-Location }
