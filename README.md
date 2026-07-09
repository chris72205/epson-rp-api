# epson-rp-api

HTTP JSON API for printing to a USB-connected **Epson TM-T88VI** receipt printer from a
**Raspberry Pi Zero W**. Print jobs are queued in memory and printed sequentially by a
background worker. No authentication — intended for a private LAN only.

## Install (on the Pi)

```sh
git clone <this repo> ~/epson-rp-api
cd ~/epson-rp-api
sudo deploy/install.sh
```

The installer sets up apt prerequisites, a venv (piwheels makes ARMv6 installs fast), a udev
rule for non-root USB access, and a systemd service that starts on boot.

If you copy the project over instead of cloning, exclude any local `venv/`
(`rsync -a --exclude venv ...`) — a venv built on another machine won't run on the Pi.
The installer detects and rebuilds a broken venv either way.

Verify the printer is visible first: `lsusb | grep 04b8` (expected: `04b8:0202`).

Deploying updates: `git pull && sudo systemctl restart epson-rp-api`
(re-run `install.sh` only when `requirements.txt` changed). Logs: `journalctl -u epson-rp-api -f`.

## API

All POST endpoints return `202 {"id": "...", "status": "queued", "status_url": "/jobs/<id>"}`
immediately, or `503 {"error": "queue full"}`. Poll the job to see the outcome. Validation
failures return `400` with a message pointing at the offending field.

| Endpoint | Description |
|---|---|
| `POST /print` | Print a block document (see below) |
| `POST /print/text` | Convenience plain-text print |
| `POST /print/test` | Print a canned test receipt exercising all features |
| `GET /jobs/<id>` | Job status: `queued` / `printing` / `done` / `failed` (+ `error`) |
| `GET /jobs` | Recent jobs, newest first (last 100) |
| `GET /status` | Printer state (connected/online/paper) + queue depth |
| `GET /health` | Liveness + version |

### POST /print — block documents

The body is `{"blocks": [...]}` — an ordered list of typed blocks, printed top to bottom.
Unknown block types or fields are rejected with a 400.

```sh
curl -X POST http://pi:8080/print -H 'Content-Type: application/json' -d '{
  "blocks": [
    {"type": "text", "content": "ACME STORE", "align": "center", "bold": true, "width": 2, "height": 2},
    {"type": "text", "content": "Item 1 .......... $4.99"},
    {"type": "feed", "lines": 1},
    {"type": "barcode", "data": "ORDER-12345"},
    {"type": "qr", "data": "https://example.com/r/123", "size": 8},
    {"type": "feed", "lines": 2},
    {"type": "cut"}
  ]
}'
```

#### Block reference (defaults shown)

**text**
```json
{"type": "text", "content": "required",
 "bold": false, "underline": 0, "invert": false,
 "align": "left", "font": "a",
 "width": 1, "height": 1,
 "newline": true}
```
`underline`: 0/1/2 · `align`: left/center/right · `font`: a/b · `width`/`height`: 1–8 size
multipliers · `newline: false` prints without a trailing line feed (lets you compose one line
from multiple styled fragments).

**feed** — `{"type": "feed", "lines": 1}` (1–20)

**cut** — `{"type": "cut", "mode": "full"}` (`full` | `partial`)

**barcode**
```json
{"type": "barcode", "data": "required", "symbology": "CODE128",
 "height": 64, "width": 3, "text_position": "below", "align": "center"}
```
`symbology`: CODE39, CODE93, CODE128, EAN13, EAN8, UPC-A, UPC-E, ITF, NW7 ·
`height`: 1–255 dots · `width`: 2–6 · `text_position`: none/above/below/both.
CODE128 data is automatically prefixed with the `{B` code-set selector if you don't provide one.

**qr**
```json
{"type": "qr", "data": "required", "size": 6, "ec": "M", "align": "center"}
```
`size`: 1–16 module size · `ec`: L/M/Q/H error correction. Rendered natively by the printer.

**image**
```json
{"type": "image", "data": "<base64 PNG/JPEG/GIF>", "align": "center",
 "width": null, "dither": true}
```
`width`: target width in dots (null = shrink to fit the 512-dot paper width, never upscaled) ·
`dither: true` uses Floyd–Steinberg; `false` uses a 50% threshold (better for line art/logos).
Transparency is flattened onto white. Request body limit is 8 MB.

```sh
curl -X POST http://pi:8080/print -H 'Content-Type: application/json' \
  -d "{\"blocks\": [{\"type\": \"image\", \"data\": \"$(base64 -w0 logo.png)\"}, {\"type\": \"cut\"}]}"
```

**drawer** — `{"type": "drawer", "pin": 2}` (2 or 5) — fires the cash-drawer kick pulse.

**beep** — `{"type": "beep", "times": 1, "duration": 3}` (each 1–9). Requires the printer's
optional internal buzzer to be present/enabled.

### POST /print/text

```sh
curl -X POST http://pi:8080/print/text -H 'Content-Type: application/json' \
  -d '{"text": "Hello from the Pi\nSecond line", "align": "left", "bold": false, "feed": 2, "cut": true}'
```

### GET /status

```json
{"printer": {"connected": true, "busy": false, "online": true, "paper": "ok"},
 "queue": {"depth": 0, "current_job": null}}
```
`paper`: `ok` / `near_end` / `out` · `busy: true` means a job is mid-print (state query skipped).

## Configuration

Environment variables (for the service, set them in `/etc/default/epson-rp-api`):

| Variable | Default | |
|---|---|---|
| `PRINTER_USB_VENDOR` / `PRINTER_USB_PRODUCT` | `0x04b8` / `0x0202` | Check with `lsusb` |
| `PRINTER_PROFILE` | `TM-T88V` | escpos capability profile (no VI entry; V is command-compatible) |
| `PAPER_WIDTH_DOTS` | `512` | 80 mm paper at 180 dpi |
| `USB_TIMEOUT_MS` | `5000` | |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | |
| `QUEUE_MAX` / `HISTORY_MAX` | `50` / `100` | |
| `LOG_LEVEL` | `INFO` | |
| `PRINTER_FAKE` | unset | `1` = no USB; jobs go to an in-memory dummy (for development) |

## Development

```sh
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest tests/
PRINTER_FAKE=1 venv/bin/python -m printapi   # run locally without a printer
```

Job queue and history are in-memory: a restart drops queued jobs and history. Clients should
treat a lost job as retryable.

## On-device verification checklist

1. `lsusb | grep 04b8` shows `04b8:0202`.
2. `curl -X POST http://localhost:8080/print/test` — full test receipt prints, then cuts.
3. `curl http://localhost:8080/status` — try with cover open, paper out, and unplugged; the
   `printer` object should reflect each state.
4. Unplug and replug the printer, then print again — the per-job USB connection recovers
   without a service restart.
5. Fire several jobs quickly and watch `GET /jobs` progress through the statuses.

## Troubleshooting

- **`USBNotFoundError` / job error "printer not connected"** — check the cable and `lsusb`;
  confirm the udev rule is installed (`ls /etc/udev/rules.d/99-epson-tm.rules`) and that you
  replugged after installing it.
- **Permission denied opening USB** — the service user must be in the `plugdev` group
  (`groups pi`), and the udev rule must match your `lsusb` IDs.
- **Kernel `usblp` driver claims the printer** (`/dev/usb/lp0` exists and prints fail) —
  python-escpos normally detaches it automatically; if not:
  `echo "blacklist usblp" | sudo tee /etc/modprobe.d/blacklist-usblp.conf` and reboot.
- **Beep block does nothing** — the TM-T88VI's buzzer is an option; enable it in the printer's
  memory switches or via the printer utility.
