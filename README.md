# AirBridge

Move photos, videos, and files between your iPhone and your Windows PC.

No app to install. No cloud. No account. Everything travels over your own
Wi-Fi and never leaves your home.

## Get started

1. Download `AirBridge-Setup.exe` from the [latest release](../../releases/latest).
2. Run it. If Windows says "Windows protected your PC", click **More info**,
   then **Run anyway**. This appears once, because the app is not code-signed.
3. A QR code pops up. Point your iPhone camera at it.

The transfer page opens in Safari. That's it.

Two one-time prompts along the way: leave "Launch AirBridge now" checked in
the installer, and allow AirBridge through Windows Firewall when asked.

## Everyday use

AirBridge lives in the system tray (bottom-right corner, sometimes behind the
`^` chevron) and starts with Windows.

- **Send to PC.** Pick photos or files on the phone. They land in your
  `AirBridge` folder on the PC.
- **On the PC.** Anything in that folder, with photo and video previews, can
  be saved back to the phone, one file at a time or all at once.
- **Links.** Toss URLs between the phone and the PC.

Click the tray icon to show the QR code again. Every time the server starts
it creates a fresh private link, so just scan again.

## If something is off

- **The phone can't connect.** Make sure both devices are on the same Wi-Fi
  and that AirBridge was allowed through Windows Firewall.
- **No QR code and no tray icon.** Check the log at
  `%LOCALAPPDATA%\AirBridge\tray.log`.

## Private by design

AirBridge only works on your local network. It never opens your network to
the internet, and only devices that scanned the QR code can connect.

---

Working on AirBridge itself? See [DEVELOPING.md](DEVELOPING.md).
Licensed under [MIT](LICENSE).
