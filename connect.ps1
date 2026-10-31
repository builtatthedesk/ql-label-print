<#
One-shot retry: find the printer, try every way of reaching it, and print a test
label down whichever one works.

Run this after changing anything on the printer side (powering it on, releasing
it from a phone, pressing its Bluetooth button).

    .\connect.ps1
    .\connect.ps1 -NoPrint      # diagnose only, do not print a test label
    .\connect.ps1 -Mac 00:11:22:33:44:55   # also try raw RFCOMM to this address
#>

[CmdletBinding()]
param(
    [string]$Mac = $(if ($env:QL_BT_MAC) { $env:QL_BT_MAC } else { '' }),
    [string]$NameLike = 'QL-',
    [switch]$NoPrint
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $here '.venv\Scripts\python.exe'
$script = Join-Path $here 'labelprint.py'
$btspp = Join-Path $here 'btspp.exe'

# No -Mac: fall back to the bt:// address in config.json, if there is one.
if (-not $Mac) {
    $cfgPath = Join-Path $here 'config.json'
    if (Test-Path $cfgPath) {
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
        foreach ($t in @($cfg.status_transport, $cfg.transport)) {
            if ($t -match '^bt://((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})$') { $Mac = $Matches[1]; break }
        }
    }
}

function Section($t) { Write-Output ''; Write-Output "--- $t ---" }

$transport = $null

# 1. Network is the least fragile, so try it first even though Bluetooth is the
#    configured route -- if the printer ever lands on the LAN, prefer it.
Section 'network'
$ips = @()
$base = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixLength -eq 24 } |
    Select-Object -First 1).IPAddress
if ($base) {
    $prefix = ($base -split '\.')[0..2] -join '.'
    $probes = foreach ($n in 1..254) {
        $a = "$prefix.$n"
        $c = New-Object System.Net.Sockets.TcpClient
        [pscustomobject]@{ ip = $a; c = $c; t = $c.ConnectAsync($a, 9100) }
    }
    Start-Sleep -Milliseconds 2500
    foreach ($p in $probes) {
        if ($p.t.IsCompleted -and -not $p.t.IsFaulted -and $p.c.Connected) { $ips += $p.ip }
        $p.c.Close()
    }
}
foreach ($ip in $ips) {
    $title = ''
    try {
        $r = Invoke-WebRequest -Uri "http://$ip/" -TimeoutSec 4 -UseBasicParsing
        if ($r.Content -match '<title>(.*?)</title>') { $title = $Matches[1].Trim() }
    } catch { }
    Write-Output "  $ip  $title"
    if ($title -match 'QL-\d' -and -not $transport) { $transport = "net://$ip" }
}
if (-not $ips) { Write-Output '  nothing on 9100' }

# 2. Raw RFCOMM, which needs no pairing.
Section 'raw RFCOMM (no pairing)'
if (-not $Mac) {
    Write-Output '  skipped: pass -Mac <printer Bluetooth address> (or set QL_BT_MAC)'
} elseif (Test-Path $btspp) {
    & $btspp probe $Mac
    if ($LASTEXITCODE -eq 0 -and -not $transport) { $transport = "bt://$Mac" }
} else {
    Write-Output "  $btspp not built"
}

# 3. Pairing, and the COM port it produces.
if (-not $transport) {
    Section 'pairing'
    & (Join-Path $here 'pair-bluetooth.ps1') -NameLike $NameLike
    $port = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'Bluetooth' -and $_.Name -match '\((COM\d+)\)' } |
        Select-Object -First 1
    if ($port -and $port.Name -match '\((COM\d+)\)') {
        Write-Output "  outgoing port: $($Matches[1])"
        $transport = "bt://$($Matches[1])"
    }
}

# ------------------------------------------------------------------ result ---
Section 'result'
if (-not $transport) {
    Write-Output 'Printer still unreachable on every transport.'
    exit 1
}

Write-Output "Reachable via: $transport"

if ($NoPrint) { exit 0 }

Write-Output ''
Write-Output 'Printing a test label ...'
& $py $script text "LABEL PRINT`nTEST OK" --label 62 --transport $transport
exit $LASTEXITCODE
