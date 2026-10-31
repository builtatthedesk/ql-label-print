// Raw RFCOMM/SPP client for the Brother QL label printer.
//
// Windows pairing (Secure Simple Pairing) times out against this printer, so
// this bypasses bonding entirely: Winsock's AF_BTH lets you connect straight to
// a remote SPP service. Passing the Serial Port Profile class GUID with port 0
// makes Winsock run the SDP query itself to find the RFCOMM channel.
//
//   btspp.exe probe  00:11:22:33:44:55
//   btspp.exe send   00:11:22:33:44:55  label.bin
//   btspp.exe status 00:11:22:33:44:55        (what roll is loaded, any errors)
//   btspp.exe status 00:11:22:33:44:55 s.bin  (also dump the 32 raw status bytes)
//   btspp.exe scan   00:11:22:33:44:55        (brute-force channels 1..30)
//
// Build:
//   csc.exe /nologo /optimize /out:btspp.exe btspp.cs

using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;

static class Bt
{
    const int AF_BTH = 32;
    const int BTHPROTO_RFCOMM = 3;
    static readonly Guid SPP = new Guid("00001101-0000-1000-8000-00805F9B34FB");

    // SOCKADDR_BTH is byte-packed (ws2bth.h uses pshpack1), so it is 30 bytes:
    //   USHORT family @0, BTH_ADDR btAddr @2, GUID serviceClassId @10, ULONG port @26.
    // Laying it out at natural alignment instead gives 40 bytes and every connect
    // fails locally with WSAEADDRNOTAVAIL (10049) before touching the radio.
    sealed class BthEndPoint : EndPoint
    {
        readonly ulong _addr;
        readonly Guid _service;
        readonly uint _port;

        public BthEndPoint(ulong addr, Guid service, uint port)
        {
            _addr = addr; _service = service; _port = port;
        }

        public override AddressFamily AddressFamily { get { return (AddressFamily)AF_BTH; } }

        public override SocketAddress Serialize()
        {
            var sa = new SocketAddress((AddressFamily)AF_BTH, 30);
            var a = BitConverter.GetBytes(_addr);
            for (int i = 0; i < 8; i++) sa[2 + i] = a[i];
            var g = _service.ToByteArray();
            for (int i = 0; i < 16; i++) sa[10 + i] = g[i];
            var p = BitConverter.GetBytes(_port);
            for (int i = 0; i < 4; i++) sa[26 + i] = p[i];
            return sa;
        }

        public override EndPoint Create(SocketAddress socketAddress) { return this; }
        public override string ToString()
        {
            return string.Format("{0:X12} svc={1} port={2}", _addr, _service, _port);
        }
    }

    static ulong ParseMac(string s)
    {
        s = s.Replace(":", "").Replace("-", "").Replace(" ", "").Trim();
        if (s.Length != 12) throw new ArgumentException("MAC must be 12 hex digits: " + s);
        ulong v = 0;
        for (int i = 0; i < 12; i += 2)
            v = (v << 8) | Convert.ToByte(s.Substring(i, 2), 16);
        return v;
    }

    static Socket Connect(ulong addr, Guid service, uint port, int timeoutMs)
    {
        var sock = new Socket((AddressFamily)AF_BTH, SocketType.Stream, (ProtocolType)BTHPROTO_RFCOMM);
        var ep = new BthEndPoint(addr, service, port);

        var result = sock.BeginConnect(ep, null, null);
        if (!result.AsyncWaitHandle.WaitOne(timeoutMs))
        {
            try { sock.Close(); } catch { }
            throw new TimeoutException("connect timed out after " + timeoutMs + "ms");
        }
        sock.EndConnect(result);
        return sock;
    }

    static string MediaType(byte b)
    {
        switch (b)
        {
            case 0x00: return "no media";
            case 0x0A: return "continuous length tape";
            case 0x0B: return "die-cut labels";
            case 0x4A: return "continuous length tape (raster)";
            case 0x4B: return "die-cut labels (raster)";
            default:   return "unknown";
        }
    }

    static string StatusType(byte b)
    {
        switch (b)
        {
            case 0x00: return "reply to status request";
            case 0x01: return "printing completed";
            case 0x02: return "error occurred";
            case 0x04: return "notification";
            case 0x05: return "phase change";
            case 0x06: return "phase change";
            default:   return "unknown";
        }
    }

    static int Main(string[] args)
    {
        if (args.Length < 2)
        {
            Console.Error.WriteLine("usage: btspp.exe <probe|send|scan> <MAC> [file]");
            return 2;
        }

        string cmd = args[0].ToLowerInvariant();
        ulong addr;
        try { addr = ParseMac(args[1]); }
        catch (Exception ex) { Console.Error.WriteLine(ex.Message); return 2; }

        Console.WriteLine("target {0:X12}", addr);

        try
        {
            if (cmd == "probe")
            {
                Console.WriteLine("connecting via SDP lookup of the SPP service class...");
                using (var s = Connect(addr, SPP, 0, 20000))
                {
                    Console.WriteLine("CONNECTED  local={0}", s.LocalEndPoint);
                    Console.WriteLine("no pairing required - RFCOMM is open");
                }
                return 0;
            }

            if (cmd == "scan")
            {
                Console.WriteLine("brute-forcing RFCOMM channels 1..30 ...");
                bool any = false;
                for (uint ch = 1; ch <= 30; ch++)
                {
                    try
                    {
                        using (var s = Connect(addr, Guid.Empty, ch, 4000))
                        {
                            Console.WriteLine("  channel {0}: OPEN", ch);
                            any = true;
                        }
                    }
                    catch (Exception ex)
                    {
                        var se = ex as SocketException;
                        string why = se != null ? se.SocketErrorCode.ToString() : ex.GetType().Name;
                        Console.WriteLine("  channel {0}: {1}", ch, why);
                    }
                    Thread.Sleep(150);
                }
                return any ? 0 : 1;
            }

            if (cmd == "status")
            {
                using (var s = Connect(addr, SPP, 0, 20000))
                {
                    Console.WriteLine("CONNECTED");
                    s.SendTimeout = 15000;
                    s.ReceiveTimeout = 15000;

                    // Clear any half-finished job, reset, then ask for status.
                    var invalidate = new byte[200];              // 200 null bytes
                    s.Send(invalidate);
                    s.Send(new byte[] { 0x1B, 0x40 });           // ESC @  initialise
                    s.Send(new byte[] { 0x1B, 0x69, 0x53 });     // ESC i S  status request

                    var buf = new byte[32];
                    int got = 0;
                    try
                    {
                        while (got < 32)
                        {
                            int n = s.Receive(buf, got, 32 - got, SocketFlags.None);
                            if (n <= 0) break;
                            got += n;
                        }
                    }
                    catch (SocketException se)
                    {
                        Console.WriteLine("receive stopped: " + se.SocketErrorCode);
                    }

                    Console.WriteLine("got {0} bytes", got);
                    if (got == 0) { Console.WriteLine("no status reply"); return 1; }

                    // Optional raw dump, so labelprint.py can decode it itself
                    // rather than scraping this text.
                    if (args.Length >= 3)
                    {
                        var trimmed = new byte[got];
                        Array.Copy(buf, trimmed, got);
                        File.WriteAllBytes(args[2], trimmed);
                        Console.WriteLine("raw bytes written to {0}", args[2]);
                    }

                    var sb = new System.Text.StringBuilder();
                    for (int i = 0; i < got; i++) sb.AppendFormat("{0:X2} ", buf[i]);
                    Console.WriteLine("raw: " + sb.ToString().Trim());

                    if (got >= 32)
                    {
                        Console.WriteLine("model code   : 0x{0:X2}", buf[4]);
                        Console.WriteLine("error info 1 : 0x{0:X2}", buf[8]);
                        Console.WriteLine("error info 2 : 0x{0:X2}", buf[9]);
                        Console.WriteLine("media width  : {0} mm", buf[10]);
                        Console.WriteLine("media type   : 0x{0:X2} ({1})", buf[11], MediaType(buf[11]));
                        Console.WriteLine("media length : {0} mm", buf[17]);
                        Console.WriteLine("status type  : 0x{0:X2} ({1})", buf[18], StatusType(buf[18]));
                        Console.WriteLine("phase        : 0x{0:X2}", buf[19]);

                        if (buf[8] != 0 || buf[9] != 0)
                            Console.WriteLine("ERROR FLAGS SET - see errorinfo bits");
                    }
                    return 0;
                }
            }

            if (cmd == "send")
            {
                if (args.Length < 3) { Console.Error.WriteLine("send needs a file"); return 2; }
                byte[] data = File.ReadAllBytes(args[2]);
                Console.WriteLine("sending {0} bytes", data.Length);

                using (var s = Connect(addr, SPP, 0, 20000))
                {
                    Console.WriteLine("CONNECTED  local={0}", s.LocalEndPoint);
                    s.SendTimeout = 30000;
                    int sent = 0;
                    while (sent < data.Length)
                    {
                        // RFCOMM MTU is small; feed it in modest chunks.
                        int chunk = Math.Min(512, data.Length - sent);
                        sent += s.Send(data, sent, chunk, SocketFlags.None);
                    }
                    Console.WriteLine("sent {0} bytes", sent);
                    Thread.Sleep(1500);   // let the printer drain before FIN
                }
                return 0;
            }

            Console.Error.WriteLine("unknown command: " + cmd);
            return 2;
        }
        catch (SocketException se)
        {
            Console.Error.WriteLine("socket error {0} ({1})", se.SocketErrorCode, se.NativeErrorCode);
            Console.Error.WriteLine(se.Message);
            return 1;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.GetType().Name + ": " + ex.Message);
            return 1;
        }
    }
}
