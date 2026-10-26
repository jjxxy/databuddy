param(
    [ValidateSet('up', 'down', 'status', 'logs', 'open')]
    [string]$Action = 'up',
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
# UTF-8 BOM is required for Chinese source text on Windows PowerShell 5.1.
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$bundle = $PSScriptRoot

function Get-LocalUrl {
    $port = 8000
    $envPath = Join-Path $bundle '.env'
    if (Test-Path -LiteralPath $envPath) {
        # Read one numeric setting only; never execute or display .env.
        foreach ($line in [System.IO.File]::ReadAllLines($envPath, $utf8)) {
            if ($line -match '^\s*(?:export\s+)?ATLAS_HTTP_PORT\s*=(.*)$') {
                $value = $Matches[1].Trim()
                if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                    ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                    $value = $value.Substring(1, $value.Length - 2)
                } else {
                    $value = ($value -split ' #', 2)[0].TrimEnd()
                }
                if ($value -match '^0*([1-9][0-9]{0,4}|0)$') {
                    $port = [int]$Matches[1]
                } else {
                    throw '.env 中 ATLAS_HTTP_PORT 需为 1–65535 的数字端口。'
                }
            }
        }
    }
    if ($port -lt 1 -or $port -gt 65535) {
        throw '.env 中 ATLAS_HTTP_PORT 需为 1–65535 的数字端口。'
    }
    return "http://localhost:$port"
}

function Open-LocalWorkspace {
    $url = Get-LocalUrl
    Write-Host "工作空间：$url"
    if (-not $NoOpen -and $env:ATLAS_NO_OPEN -ne '1') {
        try { Start-Process $url }
        catch { Write-Host '浏览器未能自动打开，请手动打开上方地址。' }
    }
}

function Invoke-Docker {
    param([string[]]$CommandArgs)
    & $script:dockerPath @CommandArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Invoke-Bootstrap {
    param([string]$Command)
    # The full path is one argument, including Chinese characters and spaces.
    # No host Python or Docker socket is needed by this temporary helper.
    $mount = "type=bind,source=$bundle,target=/bundle"
    Invoke-Docker -CommandArgs @('run', '--rm', '--network', 'none',
        '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONUTF8=1',
        '--mount', $mount, '--workdir', '/bundle',
        'python:3.12-slim', 'python', 'quickstart.py', $Command)
}

Push-Location -LiteralPath $bundle
try {
    if ($Action -eq 'open') { Open-LocalWorkspace; exit 0 }
    $dockerCommand = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        throw '请先安装并启动 Docker Desktop（Linux 容器模式），再重新运行启动脚本。'
    }
    $script:dockerPath = $dockerCommand.Source
    $dockerOS = & $script:dockerPath info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker 尚未就绪，请先启动 Docker Desktop 或 Docker Engine。' }
    if ([string]$dockerOS -ne 'linux') { throw '请将 Docker Desktop 切换为 Linux 容器模式。' }
    & $script:dockerPath compose version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '需要 Docker Compose v2，请更新 Docker Desktop 或安装 Compose 插件。' }
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'quickstart.py')) -or
        -not (Test-Path -LiteralPath (Join-Path $bundle 'compose.quickstart.yaml'))) {
        throw '启动包不完整，请保留同目录的 quickstart.py 和 compose.quickstart.yaml。'
    }
    $compose = @('compose', '--env-file', '.env', '-f', 'compose.quickstart.yaml')
    if ($Action -eq 'up') {
        Invoke-Bootstrap -Command 'setup'
        # Readiness timeout starts after image download/build has completed.
        Invoke-Docker -CommandArgs ($compose + @('up', '-d', '--build', '--wait', '--wait-timeout', '180'))
        Invoke-Bootstrap -Command 'info'
        Open-LocalWorkspace
    } else {
        if (-not (Test-Path -LiteralPath (Join-Path $bundle '.env'))) {
            throw '尚未生成启动配置，请先运行 start.cmd。'
        }
        switch ($Action) {
            'down' { Invoke-Docker -CommandArgs ($compose + @('down')) }
            'status' { Invoke-Docker -CommandArgs ($compose + @('ps', '--all')) }
            'logs' { Invoke-Docker -CommandArgs ($compose + @('logs', '--tail', '100', '--follow')) }
        }
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}
