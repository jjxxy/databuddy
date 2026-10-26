param(
    [ValidateSet('start', 'stop', 'status', 'open', 'token', 'logs')]
    [string]$Command = 'start',
    [string]$Distro = 'Ubuntu-24.04',
    [int]$Port = 8000,
    [string]$ModelConfig = '',
    [string]$OpenCodeBinary = '',
    [string]$StateDir = '',
    [string]$Python = '',
    [ValidateSet('auto', 'opencode', 'compatibility')]
    [string]$Engine = 'auto',
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:WSL_UTF8 = '1'

function Convert-ToLinuxPath([string]$Value) {
    if ($Value.StartsWith('/')) { return $Value }
    $resolved = [System.IO.Path]::GetFullPath($Value)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue' # WSL may emit a nonfatal proxy warning on stderr.
    $converted = & wsl.exe -d $Distro --exec wslpath -a -u $resolved
    $conversionCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($conversionCode -ne 0) { throw 'WSL could not convert the supplied path.' }
    return ($converted | Select-Object -Last 1).Trim()
}

try {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw 'WSL is required. See LOCAL-DEPLOYMENT.md.'
    }
    $helper = Convert-ToLinuxPath (Join-Path $PSScriptRoot 'native_local.py')
    $launch = @('-d', $Distro, '--exec', 'python3', $helper, $Command,
                '--port', [string]$Port, '--engine', $Engine, '--no-open')
    if ($ModelConfig) { $launch += @('--model-config', (Convert-ToLinuxPath $ModelConfig)) }
    if ($OpenCodeBinary) { $launch += @('--opencode-binary', (Convert-ToLinuxPath $OpenCodeBinary)) }
    if ($StateDir) { $launch += @('--state-dir', (Convert-ToLinuxPath $StateDir)) }
    if ($Python) { $launch += @('--python', (Convert-ToLinuxPath $Python)) }
    # Only start/open/status output a public URL; access credentials never enter it.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    if ($Command -in @('start', 'open', 'status')) {
        $result = & wsl.exe @launch 2>&1
        $resultCode = $LASTEXITCODE
        $ErrorActionPreference = $previousPreference
        $result | ForEach-Object { Write-Output ([string]$_) }
        if ($resultCode -eq 0 -and $Command -in @('start', 'open') -and -not $NoOpen) {
            $match = [regex]::Match(($result -join "`n"), 'http://127\.0\.0\.1:\d+')
            if ($match.Success) { Start-Process $match.Value -WindowStyle Hidden }
        }
    } else {
        & wsl.exe @launch
        $resultCode = $LASTEXITCODE
        $ErrorActionPreference = $previousPreference
    }
    exit $resultCode
} catch {
    Write-Error $_
    exit 2
}
