# AirBridge hardening and improvements

Implement these in tiers. Do one tier, run its acceptance checks, then stop for
review before the next. Keep every change consistent with CLAUDE.md (uv only, no
em dashes, LAN-only posture, no external front-end assets).

For acceptance checks, run the server with `--no-auth --port 8099 --dir ./testshare`
unless the check is specifically about auth, and curl against `http://127.0.0.1:8099`.
Clean up `testshare/` and any temp files when done.

---

## Tier 1: correctness and core hardening (do first)

### 1.1 Windows-safe filename sanitization

Problem: filenames coming from iOS can contain characters or names that NTFS
rejects, so the write fails. Harden `sanitize()` in `main.py`.

Rules, applied to the basename only:
- Replace any of `< > : " / \ | ? *` and ASCII control characters (0x00 to 0x1F)
  with `_`.
- Strip leading and trailing whitespace, then strip trailing dots and spaces
  (NTFS forbids trailing dot or space).
- If the stem (name without extension, case-insensitive) is a reserved device
  name, prefix the whole name with `_`. Reserved: `CON PRN AUX NUL` and `COM1`
  through `COM9` and `LPT1` through `LPT9`.
- If the result is empty, `.`, or `..`, use `upload.bin`.
- Cap total length at 200 characters, preserving the extension.

Acceptance:
- Upload a file named `a:b*c?.jpg`, it saves as `a_b_c_.jpg`.
- Upload a file named `CON.txt`, it saves as `_CON.txt`.
- Upload a file named `trailing. `, it does not error and has no trailing dot or space.
- Normal names are unchanged, and the existing duplicate-name suffixing still works.

### 1.2 CSRF defense in depth

Problem: a malicious page the user visits could try a cross-origin request to the
API while a session cookie exists. The `SameSite=Lax` cookie already blocks
cross-site POST and DELETE, but add a second gate.

Change: require the header `X-AirBridge: 1` on `POST /api/upload` and
`DELETE /api/files/{name}`. If it is missing, return 403. Add this header to those
two requests in `web/index.html` (the XHR upload and the fetch delete). Custom
headers force a CORS preflight that the server does not grant cross-origin, so
cross-site calls fail.

Acceptance:
- `curl -X POST .../api/upload -F "files=@README.md"` with no extra header returns 403.
- The same with `-H "X-AirBridge: 1"` returns 200.
- The web UI still uploads and deletes normally.

### 1.3 Security response headers

Add a middleware that sets these on every response:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`

The page uses an inline style block and inline script, so `'unsafe-inline'` is
required for `style-src` and `script-src`. Do not add external assets, which would
force loosening `default-src`.

Acceptance:
- `curl -I .../` shows all four headers.
- The page still renders and image previews still load.

### 1.4 Transfer logging

Print one concise line to stdout for each upload, download, delete, and
download-all, including a timestamp, the client IP (`request.client.host`), the
action, the filename, and the size where relevant. Do not log the inline preview
endpoint (`/api/raw/...`), it is too noisy. Example shape:
`[14:03:21] 192.168.1.50 UP   IMG_4821.HEIC (3.2 MB)`

Acceptance: transferring files prints matching lines in the terminal.

---

## Tier 2: robustness and UX

### 2.1 Keep the phone awake during transfers (Screen Wake Lock)

Problem: iOS can suspend a backgrounded or screen-locked Safari tab mid-transfer,
stalling a large upload.

Change in `web/index.html`: when a batch of transfers starts, request
`navigator.wakeLock.request('screen')`; release it when the queue goes idle.
Re-acquire on `visibilitychange` if the page becomes visible again while transfers
are still running. Guard for browsers without the API (feature-detect, no errors).

Acceptance: on the iPhone, a long upload keeps the screen on; on a browser without
the API there are no console errors and transfers still work.

### 2.2 Parallel uploads

Problem: uploading a large photo batch one file at a time is slow.

Change in `web/index.html`: upload with a small concurrency pool (limit 3) instead
of strictly sequentially. Preserve per-file progress bars and keep the queue order
stable. All files must still complete, and failures must still mark that file
failed without aborting the batch.

Acceptance: selecting 20 small files uploads several at once and finishes
noticeably faster; every file ends in done or failed state.

### 2.3 Optional upload size cap

Add `--max-mb N` (default 0, meaning unlimited). When set, enforce the per-file
limit while streaming the upload. On exceed: stop reading, delete the partial
file, and return HTTP 413 with a clear message. The web UI shows that file as
failed. Files under the limit are unaffected.

Acceptance: with `--max-mb 5`, a 10 MB upload returns 413 and leaves nothing on
disk; a 1 MB upload still succeeds.

---

## Tier 3: situational and optional

### 3.1 Optional HTTPS (`--https`)

Purpose: encrypt the link when running on a network you do not control. On the
home LAN this is optional.

Put the TLS dependency in an optional extra so the core install stays lean:

```toml
[project.optional-dependencies]
tls = ["cryptography>=42"]
thumbnails = ["pillow>=10", "pillow-heif>=0.16"]
```

Add `--https`. When set, generate a self-signed certificate and key on first use,
cache them under a gitignored `.airbridge/` directory, and pass them to uvicorn
via `ssl_certfile` and `ssl_keyfile`. The certificate must include the LAN IP and
`127.0.0.1` in the Subject Alternative Name, or iOS will reject it. The banner URL
becomes `https://...`. Add `.airbridge/` to `.gitignore`.

Reference implementation for the cert (verified to put the IP in the SAN):

```python
import datetime, ipaddress
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

def ensure_self_signed(ip: str, cert_path, key_path):
    if cert_path.exists() and key_path.exists():
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AirBridge")])
    san = [x509.DNSName("localhost"),
           x509.IPAddress(ipaddress.ip_address(ip)),
           x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
```

Run it with: `uv run --extra tls main.py --https`.

Acceptance: the server serves HTTPS, the banner shows an `https://` URL, and the
phone connects after accepting the one-time Safari warning. Document in README
that the warning is expected for a self-signed cert and that tapping through still
gives wire encryption.

### 3.2 Optional thumbnails (`thumbnails` extra)

Purpose: show real thumbnails for images (including HEIC) on the desktop browser,
and speed up the gallery by not sending full-size images into the list.

Add a `/api/thumb/{name}` endpoint that returns a small JPEG (longest side about
320 px) for image types, including HEIC via `pillow-heif`. Cache generated
thumbnails under `.airbridge/thumbs/`, keyed by filename and mtime, and serve the
cached copy on repeat. Import Pillow lazily: if the `thumbnails` extra is not
installed, the endpoint and the list fall back to current behavior (the list uses
`/api/raw/...` with the existing badge fallback). The list should prefer
`/api/thumb/...` only when thumbnails are available.

Wheel note: if `pillow-heif` has no wheel for the Python uv selected, pin the venv
to a supported Python (for example `uv venv --python 3.12`) per the uv on Windows
guidance, or skip this extra.

Run it with: `uv run --extra thumbnails main.py` (combine extras as
`uv run --extra tls --extra thumbnails main.py`).

Acceptance: with the extra, HEIC files show thumbnails on the desktop browser and
the gallery loads quickly; without it, behavior is unchanged.

### 3.3 README updates

Document the new flags (`--max-mb`, `--https`), the two optional extras and how to
run them, the one-time iOS certificate warning, the Wake Lock behavior, and a tip
to set a DHCP reservation for the PC so its IP and the URL stay stable across
reboots.

---

## When complete

Delete this file, and update README so it matches the final behavior.
