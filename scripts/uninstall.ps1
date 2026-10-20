$ErrorActionPreference = 'Stop'
$TaskDataRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'CodexMonitor'))
$TaskInstallDir = [System.IO.Path]::GetFullPath((Join-Path $TaskDataRoot 'app'))
$TaskRegistryKey = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.codexmonitor.reader'
$TaskManifestPath = Join-Path $TaskInstallDir 'native-host.json'
if (Test-Path -LiteralPath $TaskRegistryKey) {
    $TaskExisting = (Get-Item -LiteralPath $TaskRegistryKey).GetValue('')
    if ($TaskExisting -ne $TaskManifestPath) { throw "注册项指向其他位置，未删除：$TaskExisting" }
    Remove-Item -LiteralPath $TaskRegistryKey
}
if (Test-Path -LiteralPath $TaskInstallDir) {
    $TaskActual = Get-Item -LiteralPath $TaskInstallDir
    if ($TaskActual.LinkType) { $TaskActual = $TaskActual.ResolveLinkTarget($true) }
    if (-not $TaskActual.FullName.StartsWith($TaskDataRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '安装目录不在 CodexMonitor 目录内，未删除。' }
    $TaskManifest = Get-Content -LiteralPath $TaskManifestPath -Raw | ConvertFrom-Json
    if ($TaskManifest.name -ne 'com.codexmonitor.reader') { throw '目录不是 CodexMonitor 的安装目录，未删除。' }
    Remove-Item -LiteralPath $TaskInstallDir -Recurse -Force
}
Write-Output "读取器已卸载；历史索引保留在 $TaskDataRoot。请在 Chrome 中移除 Agent Usage Desk 扩展。"
