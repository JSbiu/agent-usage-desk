param([string]$CodexHome)
$ErrorActionPreference = 'Stop'
$TaskDataRoot = Join-Path $env:LOCALAPPDATA 'CodexMonitor'
$TaskInstallDir = Join-Path $TaskDataRoot 'app'
$TaskHostName = 'com.codexmonitor.reader'
$TaskRegistryKey = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$TaskHostName"
$TaskManifestPath = Join-Path $TaskInstallDir 'native-host.json'
$TaskManifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'extension\manifest.json') -Raw | ConvertFrom-Json
$TaskHasher = [System.Security.Cryptography.SHA256]::Create()
$TaskHash = $TaskHasher.ComputeHash([Convert]::FromBase64String($TaskManifest.key))
$TaskHasher.Dispose()
$TaskExtensionId = -join ($TaskHash[0..15] | ForEach-Object { [char](97 + ($_ -shr 4)); [char](97 + ($_ -band 15)) })
if (Test-Path -LiteralPath $TaskRegistryKey) {
    $TaskExisting = (Get-Item -LiteralPath $TaskRegistryKey).GetValue('')
    if ($TaskExisting -and $TaskExisting -ne $TaskManifestPath) {
        throw "同名读取器已注册到其他位置：$TaskExisting。请先处理该注册项。"
    }
}
if ($CodexHome -and -not (Test-Path -LiteralPath (Join-Path $CodexHome 'sessions') -PathType Container)) {
    throw 'CodexHome 必须是包含 sessions 子目录的 Codex 目录。'
}
New-Item -ItemType Directory -Path $TaskInstallDir -Force | Out-Null
foreach ($TaskFolder in @('extension', 'native', 'runtime', 'docs')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $TaskFolder) -Destination $TaskInstallDir -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'CodexMonitorHost.exe') -Destination $TaskInstallDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall.ps1') -Destination $TaskInstallDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $TaskInstallDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE') -Destination $TaskInstallDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'THIRD-PARTY.md') -Destination $TaskInstallDir -Force
$TaskNativeHost = @{ name = $TaskHostName; description = 'Agent Usage Desk local log reader'; path = (Join-Path $TaskInstallDir 'CodexMonitorHost.exe'); type = 'stdio'; allowed_origins = @("chrome-extension://$TaskExtensionId/") }
$TaskNativeHost | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $TaskManifestPath -Encoding utf8NoBOM
if ($CodexHome) { @{ home = [System.IO.Path]::GetFullPath($CodexHome) } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $TaskDataRoot 'config.json') -Encoding utf8NoBOM }
New-Item -Path $TaskRegistryKey -Force | Out-Null
Set-Item -LiteralPath $TaskRegistryKey -Value $TaskManifestPath
Write-Output 'Agent Usage Desk 本地读取器已安装（当前用户，无需管理员权限）。'
Write-Output 'Chrome 打开 chrome://extensions，启用开发者模式，点击“加载已解压的扩展程序”，选择：'
Write-Output (Join-Path $TaskInstallDir 'extension')
Write-Output "扩展 ID：$TaskExtensionId"
