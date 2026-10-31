# ql-label-print

Print labels on a Brother QL wide-format label printer (QL-1100, QL-1110NWB,
QL-1115NWB) from the command line or a small desktop window, with no
P-touch Editor and no printer driver needed. Built so scheduled tasks and
scripts can print a label unattended.

> **Unofficial.** This project is not made, endorsed or supported by Brother.
> Brother, P-touch and QL are trademarks of Brother Industries, Ltd., used here
> only to say which printers this works with. See [Disclaimer](#disclaimer).

- `labelprint.py`: renders text or an image, converts it to Brother raster
  commands with [`brother-ql-next`](https://pypi.org/project/brother-ql-next/),
  and sends it over the network, Bluetooth, USB or the Windows spooler.
- `labelgui.py`: a Tkinter window. Type, see the real label, click Print.
- `btspp.cs`: a small C# helper that talks to the printer over a raw Bluetooth
  RFCOMM socket, **with no pairing**.

Developed and tested on Windows 10 with a QL-1110NWB. The Python parts should
run elsewhere; the Bluetooth helper, spooler transport and shortcut script are
Windows-only. Off Windows, set `font` in `config.json` to a TTF that exists
(the default is Arial Bold from `C:/Windows/Fonts`).

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install brother-ql-next pillow pyserial pyusb
cp config.example.json config.json   # PowerShell: Copy-Item
```

Edit `config.json` for your printer (see [Transports](#transports)). With no
`config.json` at all, jobs go to `file://label.bin` and nothing prints.

> Use **`brother-ql-next`**, not `brother_ql`. The original package stops at the
> QL-1060N and doesn't know the QL-1100 family.

Optional Bluetooth helper (uses the C# compiler that ships with Windows):

```bash
C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe /out:btspp.exe btspp.cs
```

## Usage

```bash
.venv/Scripts/python.exe labelprint.py text "SPARE FUSES\nBasement shelf 2"
.venv/Scripts/python.exe labelprint.py image ./barcode.png --label 102x152
.venv/Scripts/python.exe labelprint.py status
```

`status` asks the printer what roll is loaded:

```
media       : 29 mm die-cut labels, 90 mm long
label id    : 29x90
errors      : none  (0x00 0x00)
...
```

Useful flags:

- `--label auto`: detect the loaded roll instead of trusting `config.json`
- `--dry-run`: build the raster but print nothing
- `--preview foo.png`: save the rendered bitmap to check it first
- `--label`: DK roll id; `--list-labels` prints them all
- `--model`: `QL-1100`, `QL-1110NWB`, ...; `--list-models` prints them all
- `--length`: length in dots for endless rolls (300 dpi is about 11.8 dots/mm)
- `--landscape` / `--portrait`: override the orientation default
- `--no-verify`: skip the pre-flight media check
- `--copies`, `--no-cut`, `--rotate`, `--font`, `--margin`

### Text is auto-fitted

The tool searches for the largest font at which the text still fits, re-wrapping
at each size. Short text prints big; long text wraps rather than shrinking.
`\n` is always a hard break. **Don't pre-wrap**: forcing short lines makes the
font smaller.

### Pre-flight media check

Before printing, the tool asks the printer what's loaded and refuses on a
mismatch, because a wrong-size job is silently rejected and leaves the printer
blinking red:

```
refusing to print: asked for DK 62 but the printer has 29 mm die-cut labels
x 90 mm loaded (DK 29x90).
Use --label 29x90, or --label auto, or --no-verify to override.
```

If the status channel can't be reached, it warns and prints anyway.

## The desktop app

```bash
powershell -ExecutionPolicy Bypass -File install-shortcut.ps1 -StartMenu
```

Puts a **Print a label** shortcut on the Desktop (and Start menu), running
`labelgui.py` with no console window, bound to **Ctrl+Alt+L**
(`-Hotkey ""` turns that off).

- The preview is the real raster, so what's on screen is what prints.
- **Roll** picks the loaded roll and writes it back to `config.json`, so the
  command line follows.
- On **endless** rolls, **Length auto** works out how much roll the text needs;
  untick it to set a fixed length.
- **Along the roll** turns the text 90° so a narrow roll makes a big label.
- Recent labels are kept in `gui-history.json` for one-click reprints.

## Transports

| Transport | Notes |
| --- | --- |
| `net://192.168.1.50` | Raw TCP port 9100. Most reliable for unattended printing. Use the printer's hostname if your router resolves it. |
| `bt://00:11:22:33:44:55` | Bluetooth by address, through `btspp.exe`. No pairing needed. |
| `bt://COM5` | Bluetooth through a paired outgoing COM port. |
| `usb://0x04f9:0x20c0` | pyusb. Needs a WinUSB/libusb driver (e.g. via Zadig), which replaces Brother's driver for that device. |
| `spool://Brother QL-1110NWB` | Raw job through a Windows print queue. No driver swap; one-way, so no media check. |
| `file://out.bin` | Dry run: writes the raster to disk. |

`discover.ps1` sweeps the network, Bluetooth and USB and prints a transport
string to paste. `connect.ps1` tries every route and prints a test label down
whichever one answers.

### Printing over the network, status over Bluetooth

On the QL-1110NWB, port 9100 accepts print jobs but **never answers a status
request**. Bluetooth does. So you can print over the network and ask for status
over Bluetooth:

```json
"transport":        "net://192.168.1.50",
"status_transport": "bt://00:11:22:33:44:55"
```

## Things learned the hard way

- **There's no "QL-1100NWB".** Resellers (and brother-ql-next's model list) use the name, but the real models are
  the **QL-1100** (USB only) and the **QL-1110NWB** (USB, Ethernet, Wi-Fi,
  Bluetooth). Check the sticker before choosing a transport.
- **Don't bother pairing over Bluetooth.** On our unit, Windows pairing always
  timed out, but a raw RFCOMM socket to the printer's serial service works
  without it. That's what `btspp.exe` does. `pair-bluetooth.ps1` is kept as a
  diagnostic only.
- **`SOCKADDR_BTH` is byte-packed (30 bytes).** Declared with normal alignment
  it's 40 bytes and every connect fails locally with `AddressNotAvailable`,
  which looks exactly like a dead printer.
- **A red blinking power light** after a wrong-size job is latched: open and
  close the roll cover (or power-cycle) before it will answer status again.
- **The network side can hang while Bluetooth looks fine.** We saw the printer
  stay on Wi-Fi at layer 2 (ARP entry present) while TCP timed out, and jobs
  reported `sent N bytes` but nothing printed. Only a power cycle fixed the
  network side. Printing over `bt://` still worked in the meantime.
- **`sent N bytes` means the bytes left this computer**, not that a label came
  out.
- **Bluetooth is one link at a time.** If a phone app is connected, the PC can't
  connect.
- **Endless rolls have a minimum length** of 301 dots (about 25.5 mm) on these
  heads, and short labels are padded up to it. Die-cut rolls smaller than that
  (e.g. 23x23) print fine.

## Disclaimer

This is an independent project. It is not affiliated with, endorsed by or
supported by Brother Industries, Ltd. or any of its subsidiaries. Brother,
P-touch, QL and DK are trademarks of Brother Industries, Ltd. Model and label
names are used only to describe compatibility. For official software, drivers
and support, go to [Brother](https://support.brother.com/).

The printer protocol handling comes from
[`brother-ql-next`](https://github.com/LunarEclipse363/brother_ql_next), a fork
of [`brother_ql`](https://github.com/pklaus/brother_ql) by Philipp Klaus, used
under the GPL-3.0. No Brother software, firmware or documentation is included in
this repository. Other dependencies (Pillow, pySerial, PyUSB) are installed
separately under their own licenses.

No warranty. This sends raw commands to hardware. Use at your own risk.

## License

GPL-3.0. See [LICENSE](LICENSE).
