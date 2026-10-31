<#
Find a Brother QL label printer on whichever transport it happens to be using.

Checks all three in one pass:
  * network  -- sweeps the local /24 for anything answering on raw port 9100
  * usb      -- looks for Brother's VID (0x04F9) on the USB bus
  * bluetooth-- lists paired and in-range BT devices, plus outgoing COM ports

Prints a ready-to-paste --transport string for anything it recognises.
#>

[CmdletBinding()]
param(
    [string]$Subnet,
    [int]$TimeoutMs = 2500
)

function Write-Section($name) { Write-Output ''; Write-Output "=== $name ===" }

# ---------------------------------------------------------------- network ---
Write-Section 'NETWORK (raw port 9100)'

if (-not $Subnet) {
    $ip = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixLength -eq 24 } |
        Select-Object -First 1
    if ($ip) { $Subnet = ($ip.IPAddress -split '\.')[0..2] -join '.' }
}

if (-not $Subnet) {
    Write-Output '  no /24 interface found; pass -Subnet 192.168.1'
} else {
    Write-Output "  sweeping $Subnet.0/24 ..."
    $probes = foreach ($n in 1..254) {
        $addr = "$Subnet.$n"
        $client = New-Object System.Net.Sockets.TcpClient
        [pscustomobject]@{ ip = $addr; client = $client; task = $client.ConnectAsync($addr, 9100) }
    }
    Start-Sleep -Milliseconds $TimeoutMs

    $found = @()
    foreach ($p in $probes) {
        if ($p.task.IsCompleted -and -not $p.task.IsFaulted -and $p.client.Connected) { $found += $p.ip }
        $p.client.Close()
    }

    if (-not $found) { Write-Output '  (nothing listening on 9100)' }
    foreach ($addr in $found) {
        $title = ''
        try {
            $r = Invoke-WebRequest -Uri "http://$addr/" -TimeoutSec 5 -UseBasicParsing
            if ($r.Content -match '<title>(.*?)</title>') { $title = $Matches[1].Trim() }
        } catch { $title = '(no web UI)' }

        if ($title -match 'QL-\d') {
            Write-Output "  *** $addr  $title   -->  --transport net://$addr"
        } else {
            Write-Output "      $addr  $title"
        }
    }
}

# -------------------------------------------------------------------- usb ---
Write-Section 'USB (Brother VID 04F9)'

$usb = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_04F9' }
if (-not $usb) {
    Write-Output '  (no Brother device on the USB bus)'
} else {
    foreach ($d in $usb) {
        $productId = if ($d.InstanceId -match 'PID_([0-9A-Fa-f]{4})') { $Matches[1] } else { '????' }
        Write-Output "  *** $($d.FriendlyName)  [$($d.Status)]  -->  --transport usb://0x04f9:0x$productId"
    }
    Write-Output '  note: the pyusb backend needs a WinUSB/libusb driver bound to the device.'
}

# -------------------------------------------------------------- bluetooth ---
Write-Section 'BLUETOOTH'

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($WinRtTask, $ResultType) {
    $netTask = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
$null = [Windows.Devices.Bluetooth.BluetoothDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]

foreach ($paired in @($true, $false)) {
    $label = if ($paired) { 'paired' } else { 'in range, unpaired' }
    try {
        $sel = [Windows.Devices.Bluetooth.BluetoothDevice]::GetDeviceSelectorFromPairingState($paired)
        $devs = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($sel)) ([Windows.Devices.Enumeration.DeviceInformationCollection])
        if ($devs.Count -eq 0) {
            Write-Output "  ${label}: (none)"
        } else {
            foreach ($d in $devs) {
                $mark = if ($d.Name -match 'QL-\d|Brother') { '***' } else { '   ' }
                Write-Output "  $mark ${label}: $($d.Name)"
            }
        }
    } catch {
        Write-Output "  ${label}: ERR $($_.Exception.Message)"
    }
}

Write-Section 'SERIAL PORTS (Bluetooth SPP shows up here once paired)'
$ports = Get-CimInstance Win32_SerialPort -ErrorAction SilentlyContinue
if (-not $ports) {
    Write-Output '  (no COM ports)'
} else {
    foreach ($p in $ports) {
        $mark = if ($p.Description -match 'Bluetooth') { '***' } else { '   ' }
        Write-Output "  $mark $($p.DeviceID)  $($p.Description)"
    }
    Write-Output '  a paired QL gives you TWO ports; the OUTGOING one is the one to print to.'
}

Write-Output ''
