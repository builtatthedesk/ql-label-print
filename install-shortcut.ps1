<#
.SYNOPSIS
    Put a "Print a label" shortcut on the Desktop (and optionally the Start menu).

.DESCRIPTION
    Points at pythonw.exe in this project's venv so the window opens with no
    console behind it. Re-run it any time; it overwrites the shortcut.

.PARAMETER Hotkey
    Global shortcut key, e.g. CTRL+ALT+L. Windows only honours these on
    shortcuts that live on the Desktop or in the Start menu. "" disables it.

.PARAMETER StartMenu
    Also drop a copy in the Start menu so it shows up when you type "label".
#>
param(
    [string]$Hotkey = "CTRL+ALT+L",
    [switch]$StartMenu
)

$ErrorActionPreference = "Stop"

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $here ".venv\Scripts\pythonw.exe"
$script  = Join-Path $here "labelgui.py"
$icon    = Join-Path $here "label.ico"

foreach ($p in @($pythonw, $script)) {
    if (-not (Test-Path $p)) { throw "missing $p" }
}

function New-LabelShortcut([string]$Path) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath       = $pythonw
    $lnk.Arguments        = '"{0}"' -f $script
    $lnk.WorkingDirectory = $here
    $lnk.Description      = "Print a label on the Brother QL-1110NWB"
    if (Test-Path $icon) { $lnk.IconLocation = $icon }
    if ($Hotkey) { $lnk.Hotkey = $Hotkey }
    $lnk.Save()
    Write-Host "created $Path"
}

New-LabelShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Print a label.lnk")

if ($StartMenu) {
    $programs = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
    New-LabelShortcut (Join-Path $programs "Print a label.lnk")
}

if ($Hotkey) { Write-Host "hotkey: $Hotkey" }
