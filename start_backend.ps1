param(
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8000,
    [switch]$RestartExisting = $true
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "[INFO] 项目目录: $root"
$displayHost = if ($BindHost -eq "0.0.0.0") { "localhost" } else { $BindHost }
Write-Host "[INFO] 启动后端: http://$displayHost`:$Port"
Write-Host "[INFO] 按 Ctrl+C 可停止服务"

if ($RestartExisting) {
    Write-Host "[INFO] 启动前先清理旧的后端 / Solver 进程"
    & "$root\stop_backend.ps1" -BackendPort $Port -SolverPort 8889 -FullStop 0
}

$env:HOST = $BindHost
$env:PORT = [string]$Port

# Ensure uv is available
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Error "未找到 uv 命令。请先安装 uv: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
}

Write-Host "[INFO] Using uv: $(uv --version)"
uv run main.py
