param([Parameter(Mandatory=$true)][string]$ProjectRoot)
$ErrorActionPreference = 'Stop'
$desktopPath = [Environment]::GetFolderPath('Desktop')
if (-not $desktopPath) { throw 'Windows Desktop folder was not found.' }
$shortcutDirectory = Join-Path $desktopPath 'clickables'
New-Item -ItemType Directory -Force -Path $shortcutDirectory | Out-Null
$shortcutShell = New-Object -ComObject WScript.Shell
foreach ($item in @('1 - Setup','2 - Monitor','3 - Start Ember','4 - Stop Ember')) {
    $target = Join-Path $ProjectRoot ('devops\clickables\Windows\' + $item + '.cmd')
    $shortcut = $shortcutShell.CreateShortcut((Join-Path $shortcutDirectory ($item + '.lnk')))
    $shortcut.TargetPath = $target
    $shortcut.WorkingDirectory = Split-Path -Parent $target
    $shortcut.Description = 'Ember desktop development - ' + $item
    $shortcut.Save()
}
Write-Host ('Desktop clickables: ' + $shortcutDirectory)
