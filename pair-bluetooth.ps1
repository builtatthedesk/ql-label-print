<#
Pair the Brother QL label printer over Bluetooth, and bind its SPP service so
Windows creates the outgoing COM port that labelprint.py prints to.

Two notes on why this is P/Invoke and not something tidier:

  * The modern WinRT path is unusable from PowerShell 5.1. Association endpoints
    from DeviceInformation.FindAllAsync come back with CanPair=false for BR/EDR
    devices, so pairing must run against a live DeviceWatcher result -- but PS
    cannot bind WinRT TypedEventHandler delegates, and Add-Type cannot reference
    the .winmd files needed to do it in C# (no reference assemblies installed).

  * The legacy BluetoothAuthenticateDevice(PIN) call times out (258) against this
    printer, because a Bluetooth 2.1+EDR device negotiates Secure Simple Pairing
    rather than a legacy PIN. SSP requires registering an authentication callback
    and answering the ceremony, which is what the Ex API below does.

Usage:
    .\pair-bluetooth.ps1                  # find any QL-*, pair it, bind SPP
    .\pair-bluetooth.ps1 -Unpair          # remove the pairing again
    .\pair-bluetooth.ps1 -Pin 1234        # only used for a legacy PIN ceremony
#>

[CmdletBinding()]
param(
    [string]$NameLike = 'QL-',
    [string]$Pin = '0000',
    [switch]$Unpair,
    [int]$InquirySeconds = 15
)

$cs = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class BtPair
{
    // ------------------------------------------------------------ structs ---
    [StructLayout(LayoutKind.Sequential)]
    public struct SYSTEMTIME { public ushort y, m, dow, d, h, min, s, ms; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct BLUETOOTH_DEVICE_INFO
    {
        public int dwSize;
        public ulong Address;
        public uint ulClassofDevice;
        [MarshalAs(UnmanagedType.Bool)] public bool fConnected;
        [MarshalAs(UnmanagedType.Bool)] public bool fRemembered;
        [MarshalAs(UnmanagedType.Bool)] public bool fAuthenticated;
        public SYSTEMTIME stLastSeen;
        public SYSTEMTIME stLastUsed;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 248)] public string szName;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct BLUETOOTH_DEVICE_SEARCH_PARAMS
    {
        public int dwSize;
        [MarshalAs(UnmanagedType.Bool)] public bool fReturnAuthenticated;
        [MarshalAs(UnmanagedType.Bool)] public bool fReturnRemembered;
        [MarshalAs(UnmanagedType.Bool)] public bool fReturnUnknown;
        [MarshalAs(UnmanagedType.Bool)] public bool fReturnConnected;
        [MarshalAs(UnmanagedType.Bool)] public bool fIssueInquiry;
        public byte cTimeoutMultiplier;
        public IntPtr hRadio;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct BLUETOOTH_FIND_RADIO_PARAMS { public int dwSize; }

    // BLUETOOTH_AUTHENTICATE_RESPONSE. The middle is a union of pin / oob /
    // numeric-comparison / passkey info, 32 bytes wide (oob is the largest).
    [StructLayout(LayoutKind.Explicit)]
    public struct BLUETOOTH_AUTHENTICATE_RESPONSE
    {
        [FieldOffset(0)]  public ulong bthAddressRemote;
        [FieldOffset(8)]  public uint authMethod;
        [FieldOffset(12)] public uint numericOrPasskey;   // numericCompInfo / passkeyInfo
        [FieldOffset(12)] public byte pin0;               // pinInfo.pin[0], filled via the buffer
        [FieldOffset(28)] public byte pinLength;          // pinInfo.pinLength (pin[16] then length)
        [FieldOffset(44)] public byte negativeResponse;
    }

    public const uint AUTH_METHOD_LEGACY               = 1;
    public const uint AUTH_METHOD_OOB                  = 2;
    public const uint AUTH_METHOD_NUMERIC_COMPARISON   = 3;
    public const uint AUTH_METHOD_PASSKEY_NOTIFICATION = 4;
    public const uint AUTH_METHOD_PASSKEY              = 5;

    // ------------------------------------------------------------ imports ---
    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern IntPtr BluetoothFindFirstRadio(ref BLUETOOTH_FIND_RADIO_PARAMS p, out IntPtr hRadio);
    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern bool BluetoothFindRadioClose(IntPtr hFind);

    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern IntPtr BluetoothFindFirstDevice(ref BLUETOOTH_DEVICE_SEARCH_PARAMS p, ref BLUETOOTH_DEVICE_INFO di);
    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern bool BluetoothFindNextDevice(IntPtr hFind, ref BLUETOOTH_DEVICE_INFO di);
    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern bool BluetoothFindDeviceClose(IntPtr hFind);

    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern uint BluetoothRemoveDevice(ref ulong pAddress);

    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern uint BluetoothSetServiceState(IntPtr hRadio, ref BLUETOOTH_DEVICE_INFO di,
        ref Guid pGuidService, uint dwServiceFlags);

    public delegate bool AuthCallbackEx(IntPtr pvParam, IntPtr pAuthCallbackParams);

    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern uint BluetoothRegisterForAuthenticationEx(ref BLUETOOTH_DEVICE_INFO pbtdiIn,
        out IntPtr phRegHandle, AuthCallbackEx pfnCallback, IntPtr pvParam);
    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern bool BluetoothUnregisterAuthentication(IntPtr hRegHandle);

    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern uint BluetoothSendAuthenticationResponseEx(IntPtr hRadioIn,
        ref BLUETOOTH_AUTHENTICATE_RESPONSE pauthResponse);

    [DllImport("bthprops.cpl", SetLastError = true)]
    public static extern uint BluetoothAuthenticateDeviceEx(IntPtr hwndParentIn, IntPtr hRadioIn,
        ref BLUETOOTH_DEVICE_INFO pbtdiInout, IntPtr pbtOobData, uint authenticationRequirement);

    public const uint MITM_PROTECTION_NOT_REQUIRED = 0;

    public const uint BLUETOOTH_SERVICE_DISABLE = 0x00;
    public const uint BLUETOOTH_SERVICE_ENABLE  = 0x01;

    // ------------------------------------------------------------ helpers ---
    static StringBuilder _log = new StringBuilder();
    static void Log(string s) { lock (_log) _log.AppendLine(s); }
    public static string Drain() { lock (_log) { var s = _log.ToString(); _log.Clear(); return s; } }

    public static int DeviceInfoSize { get { return Marshal.SizeOf(typeof(BLUETOOTH_DEVICE_INFO)); } }

    public static IntPtr GetRadio()
    {
        var p = new BLUETOOTH_FIND_RADIO_PARAMS();
        p.dwSize = Marshal.SizeOf(typeof(BLUETOOTH_FIND_RADIO_PARAMS));
        IntPtr hRadio;
        IntPtr hFind = BluetoothFindFirstRadio(ref p, out hRadio);
        if (hFind == IntPtr.Zero) return IntPtr.Zero;
        BluetoothFindRadioClose(hFind);
        return hRadio;
    }

    public static BLUETOOTH_DEVICE_INFO[] Discover(IntPtr hRadio, byte timeoutMultiplier)
    {
        var sp = new BLUETOOTH_DEVICE_SEARCH_PARAMS();
        sp.dwSize = Marshal.SizeOf(typeof(BLUETOOTH_DEVICE_SEARCH_PARAMS));
        sp.fReturnAuthenticated = true;
        sp.fReturnRemembered = true;
        sp.fReturnUnknown = true;
        sp.fReturnConnected = true;
        sp.fIssueInquiry = true;
        sp.cTimeoutMultiplier = timeoutMultiplier;
        sp.hRadio = hRadio;

        var di = new BLUETOOTH_DEVICE_INFO();
        di.dwSize = DeviceInfoSize;

        var list = new System.Collections.Generic.List<BLUETOOTH_DEVICE_INFO>();
        IntPtr hFind = BluetoothFindFirstDevice(ref sp, ref di);
        if (hFind == IntPtr.Zero) return list.ToArray();
        do {
            list.Add(di);
            di = new BLUETOOTH_DEVICE_INFO();
            di.dwSize = DeviceInfoSize;
        } while (BluetoothFindNextDevice(hFind, ref di));
        BluetoothFindDeviceClose(hFind);
        return list.ToArray();
    }

    public static string Mac(ulong addr)
    {
        var b = BitConverter.GetBytes(addr);
        return string.Format("{0:X2}:{1:X2}:{2:X2}:{3:X2}:{4:X2}:{5:X2}",
            b[5], b[4], b[3], b[2], b[1], b[0]);
    }

    // ---------------------------------------------------- SSP ceremony ------
    // The callback fires on a Bluetooth service thread while
    // BluetoothAuthenticateDeviceEx is blocking, so keep the delegate rooted.
    static AuthCallbackEx _cb;
    static IntPtr _hRadioForCb;
    static string _legacyPin;

    public static uint PairSsp(IntPtr hRadio, ref BLUETOOTH_DEVICE_INFO di, string legacyPin)
    {
        _hRadioForCb = hRadio;
        _legacyPin = legacyPin;
        _cb = new AuthCallbackEx(OnAuth);

        IntPtr hReg;
        uint rc = BluetoothRegisterForAuthenticationEx(ref di, out hReg, _cb, IntPtr.Zero);
        Log("  RegisterForAuthenticationEx -> " + rc);
        if (rc != 0) return rc;

        try
        {
            uint auth = BluetoothAuthenticateDeviceEx(IntPtr.Zero, hRadio, ref di,
                                                      IntPtr.Zero, MITM_PROTECTION_NOT_REQUIRED);
            Log("  AuthenticateDeviceEx -> " + auth);
            return auth;
        }
        finally
        {
            BluetoothUnregisterAuthentication(hReg);
            GC.KeepAlive(_cb);
        }
    }

    static bool OnAuth(IntPtr pvParam, IntPtr pParams)
    {
        try
        {
            int infoSize = DeviceInfoSize;
            ulong addr    = (ulong)Marshal.ReadInt64(pParams, 8);   // deviceInfo.Address
            uint  method  = (uint)Marshal.ReadInt32(pParams, infoSize);
            uint  ioCap   = (uint)Marshal.ReadInt32(pParams, infoSize + 4);
            uint  authReq = (uint)Marshal.ReadInt32(pParams, infoSize + 8);
            uint  numeric = (uint)Marshal.ReadInt32(pParams, infoSize + 12);

            Log("  ceremony: method=" + method + " ioCap=" + ioCap +
                " authReq=" + authReq + " numeric=" + numeric);

            var resp = new BLUETOOTH_AUTHENTICATE_RESPONSE();
            resp.bthAddressRemote = addr;
            resp.authMethod = method;
            resp.negativeResponse = 0;

            if (method == AUTH_METHOD_NUMERIC_COMPARISON)
            {
                resp.numericOrPasskey = numeric;      // "yes, the numbers match"
            }
            else if (method == AUTH_METHOD_PASSKEY || method == AUTH_METHOD_PASSKEY_NOTIFICATION)
            {
                resp.numericOrPasskey = numeric;
            }
            else if (method == AUTH_METHOD_LEGACY)
            {
                // pinInfo.pin is a 16-byte array at offset 12, length at 28.
                var buf = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(BLUETOOTH_AUTHENTICATE_RESPONSE)));
                try
                {
                    Marshal.StructureToPtr(resp, buf, false);
                    byte[] pinBytes = Encoding.ASCII.GetBytes(_legacyPin);
                    Marshal.Copy(pinBytes, 0, buf + 12, pinBytes.Length);
                    Marshal.WriteByte(buf, 28, (byte)pinBytes.Length);
                    resp = (BLUETOOTH_AUTHENTICATE_RESPONSE)Marshal.PtrToStructure(
                        buf, typeof(BLUETOOTH_AUTHENTICATE_RESPONSE));
                }
                finally { Marshal.FreeHGlobal(buf); }
            }

            uint rc = BluetoothSendAuthenticationResponseEx(_hRadioForCb, ref resp);
            Log("  SendAuthenticationResponseEx -> " + rc);
            return true;
        }
        catch (Exception ex)
        {
            Log("  callback exception: " + ex.Message);
            return false;
        }
    }
}
'@

Add-Type -TypeDefinition $cs -Language CSharp -ErrorAction Stop

# Serial Port Profile. Enabling this is what makes Windows create the COM port.
$SPP_GUID = [Guid]'00001101-0000-1000-8000-00805F9B34FB'

$hRadio = [BtPair]::GetRadio()
if ($hRadio -eq [IntPtr]::Zero) { Write-Output 'No Bluetooth radio found.'; exit 1 }
Write-Output "Radio handle: $hRadio   (BLUETOOTH_DEVICE_INFO = $([BtPair]::DeviceInfoSize) bytes)"

$mult = [byte][Math]::Min(48, [Math]::Max(1, [Math]::Round($InquirySeconds / 1.28)))
Write-Output "Inquiring for ~$([Math]::Round($mult * 1.28))s ..."
$devices = [BtPair]::Discover($hRadio, $mult)

Write-Output "Devices seen: $($devices.Count)"
foreach ($d in $devices) {
    Write-Output ("  {0,-24} {1}  auth={2} remembered={3} connected={4} class=0x{5:X}" -f `
            $d.szName, [BtPair]::Mac($d.Address), $d.fAuthenticated, $d.fRemembered, $d.fConnected, $d.ulClassofDevice)
}

$target = $devices | Where-Object { $_.szName -like "*$NameLike*" } | Select-Object -First 1
if (-not $target) {
    Write-Output ''
    Write-Output "No device matching '*$NameLike*'. Is it powered on with Bluetooth enabled?"
    exit 1
}

Write-Output ''
Write-Output "Target: $($target.szName)  $([BtPair]::Mac($target.Address))"

# ----------------------------------------------------------------- unpair ---
if ($Unpair) {
    $addr = $target.Address
    $rc = [BtPair]::BluetoothRemoveDevice([ref]$addr)
    Write-Output "BluetoothRemoveDevice -> $rc  (0 = success)"
    exit ([int]($rc -ne 0))
}

# ------------------------------------------------------------------- pair ---
if ($target.fAuthenticated) {
    Write-Output 'Already paired.'
} else {
    Write-Output 'Pairing via Secure Simple Pairing ...'
    $rc = [BtPair]::PairSsp($hRadio, [ref]$target, $Pin)
    Write-Output ([BtPair]::Drain())
    if ($rc -ne 0) {
        Write-Output "Pairing failed, rc=$rc"
        Write-Output '   258 WAIT_TIMEOUT        - printer did not answer; is it in pairing mode?'
        Write-Output '  1244 NOT_AUTHENTICATED   - ceremony rejected'
        Write-Output '     5 ACCESS_DENIED       - needs an elevated shell'
        exit 1
    }
    Write-Output 'Paired.'
}

# ------------------------------------------------------- bind the SPP port ---
Write-Output ''
Write-Output 'Enabling Serial Port Profile (creates the COM port) ...'
$guid = $SPP_GUID
$rc = [BtPair]::BluetoothSetServiceState($hRadio, [ref]$target, [ref]$guid, [BtPair]::BLUETOOTH_SERVICE_ENABLE)
Write-Output "BluetoothSetServiceState -> $rc  (0 = success)"

Start-Sleep -Seconds 4

Write-Output ''
Write-Output '=== COM ports ==='
Get-CimInstance Win32_SerialPort -ErrorAction SilentlyContinue |
    ForEach-Object { Write-Output "  $($_.DeviceID)  $($_.Description)" }
Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '\(COM\d+\)' } |
    ForEach-Object { Write-Output "  $($_.Name)" }
Write-Output ''
Write-Output 'The OUTGOING Bluetooth port is the one to use: --transport bt://COMn'
