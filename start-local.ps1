param(
    [ValidateSet('up', 'down', 'status', 'logs', 'open')]
    [string]$Action = 'up',
    [switch]$NoOpen,
    [string]$Workspace,
    [switch]$ChooseFolder,
    [switch]$Offline
)

$ErrorActionPreference = 'Stop'
# UTF-8 BOM is required for Chinese source text on Windows PowerShell 5.1.
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$bundle = $PSScriptRoot

$folderState = Join-Path $bundle '.atlas-local-folder'
if (-not $Workspace -and -not $ChooseFolder -and (Test-Path -LiteralPath $folderState)) {
    $Workspace = [System.IO.File]::ReadAllText($folderState, $utf8).TrimEnd([char]13, [char]10)
}
if ($Action -eq 'up' -and (-not $Workspace -or $ChooseFolder)) {
    Add-Type -AssemblyName System.Windows.Forms
    $picker = New-Object System.Windows.Forms.FolderBrowserDialog
    $picker.Description = '选择 Atlas 业务工作文件夹：原文件不会被覆盖，生成内容保存在独立子目录'
    $picker.ShowNewFolderButton = $true
    if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }
    $Workspace = $picker.SelectedPath
    $picker.Dispose()
}
if ($Workspace) {
    $folder = Get-Item -LiteralPath $Workspace -Force
    if (-not $folder.PSIsContainer -or ($folder.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or
        $folder.FullName -eq [System.IO.Path]::GetPathRoot($folder.FullName)) {
        throw '请选择普通业务文件夹，不能是磁盘根目录或目录链接。'
    }
    $Workspace = $folder.FullName
    [System.IO.File]::WriteAllText($folderState, $Workspace + "`n", $utf8)
    $env:ATLAS_LOCAL_WORKSPACE_HOST = $Workspace
    $env:ATLAS_LOCAL_LABEL = $folder.Name
    $env:ATLAS_LOCAL_UID = '10001'
    $env:ATLAS_LOCAL_GID = '10001'
} elseif ($Action -ne 'open') { throw '尚未选择本地工作文件夹，请先运行 start-local.cmd。' }
if ($Offline) { $env:ATLAS_NETWORK_MODE = 'local_only' }
elseif (-not $env:ATLAS_NETWORK_MODE) { $env:ATLAS_NETWORK_MODE = 'connected' }

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
    $quotedBundle = $bundle.Replace('"', '""')
    $mount = 'type=bind,"source=' + $quotedBundle + '",target=/bundle'
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
        -not (Test-Path -LiteralPath (Join-Path $bundle 'compose.local.yaml'))) {
        throw '启动包不完整，请保留同目录的 quickstart.py 和 compose.local.yaml。'
    }
    $compose = @('compose', '--env-file', '.env', '-f', 'compose.local.yaml')
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
