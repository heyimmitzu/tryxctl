# TRYX Panorama (cm01_se) — Host Protocol Reference

Reverse-engineered from the on-device firmware (`HomeUI.apk`, `SerialService.apk`)
and from ground-truth KANALI traffic captured in the device's own logs
(`home_log.txt` / `serialserver_log.txt`).

## 1. Device identity

| Item | Value |
|------|-------|
| Board | Rockchip (SoC), Android 11 (SDK 30) |
| Brand / product | `TRYX` / `cm01_se` |
| OEM (display controller) | `baiyi` |
| USB VID:PID | `18d1:2d04` (reuses Google AOA VID/PID) |
| USB config | `acm_adb` (CDC-ACM serial + ADB) |
| Serial (host) | `/dev/ttyACM0` @ **115200** 8N1 |
| Serial (device) | `/dev/ttyGS0` (USB gadget) |
| Media dir | `sdcard/pcMedia/` (== `/sdcard/pcMedia/`) |
| Bundled themes | `/system/media/video/*.mp4`, `/system/media/anim/*.webp` |

The display panel is **1080 px tall**; the built-in ratio presets are
`1:1 → 1120×1080`, `3:2 → 1620×1080`, `4:3 → 1440×1080`, `16:9 → 1920×1080`,
so the native panel is **1920×1080 (16:9)** (confirmed). Real KANALI configs set
`"rotate": 270` (portrait mounting) — rotation is a *separate* persistent
setting, not part of the media command.

## 2. Serial framing

Every message is a UTF-8 "HTTP-like" document wrapped in a framed +
byte-stuffed envelope:

```
0x5A | len_hi len_lo | content... | crc | 0x5A
```

* `len` — 2 bytes **big-endian** = `len(content) + 5`.
* `crc` — 1 byte = `(sum of [len_hi len_lo content...]) & 0xFF` (simple 8-bit sum).
* **Byte-stuffing** is applied to everything between the `0x5A` delimiters:
  `0x5A → 0x5B 0x01`, `0x5B → 0x5B 0x02`.
* Receive side un-stuffs and discards the CRC (the device does not verify it).

## 3. Message content (HTTP-like)

```
<METHOD> <CMD> <VERSION>\r\n
HeaderKey=Value\r\n
...\r\n
\r\n
<body>
```

* `METHOD` ∈ `GET | POST | STATE | DELETE`
* `VERSION` = `1`
* Headers are `DataHeader` fields serialized as `Key=Value` (omit `-1` values):
  `SeqNumber`, `AckNumber`, `ContentLength`, `ContentType`, `FileName`,
  `FileSize`, `ContentRange`, `Counter`, `Date`, `msgId`.
* The body is everything after the first `\r\n\r\n` (JSON for our commands).

Example (handshake):

```
POST conn 1\r\n
SeqNumber=1\r\nContentType=json\r\nContentLength=0\r\n
\r\n
```

Sequencing: host sets `SeqNumber=N`; the device ACKs with
`AckNumber=N+1` (request line `1 200`).

## 4. Commands (host → device)

All display/config commands are `POST <cmd> 1` with a JSON body.

| cmd | Purpose / body |
|-----|----------------|
| `conn` | Handshake. Replies `{"attribute":[...],"OS":"Android","productId":"cm01","version":{"app","firmware","hardware"},"sn"}` |
| `transport` | Begin file transfer. `{"fileName","fileSize","type"}` → opens `sdcard/pcMedia/<fileName>`, replies `{"state":"success","blockMaxSize":888888888}` |
| *(raw bytes)* | After `transport`, raw (unframed) file bytes are written to the open file until `fileSize` reached |
| `transported` | End transfer. `{"fileName","md5"}` |
| `mediaDelete` | `{"include":[names]}` or `{"exclude":[names]}` (delete in media dir) |
| `waterBlockScreenId` | Body = a `ScreenConfig` (below). Switches the water-block screen. |
| `preset` | Body = a `ScreenConfig`. |
| `config` | Full config. `{"temperature","waterBlockScreen":{"enable","displayInSleep","waterfallMode","brightness","rotate","id":<ScreenConfig>,"fanLCD":{...}},"spec":{"cpu","gpu"}}` |
| `turboPump` | `{"enable":bool,"value":int}` |
| `rotate` | `{"degree":N}` (writes `persist.vendor.orientation`) |
| `brightness` | `{"value":0..100}` |
| `displayInSleep` | `{"enable":bool}` |
| `waterfallMode` | `{"enable":bool}` |
| `sysinfoDisplay` | `{"items":["Cpu Usage",...]}` |
| `spec` | `{"cpu","gpu"}` |
| `temperature` | `{"value":"Celsius"|"Fahrenheit"}` |
| `power` | `{"event":"suspend"|"shutdown"|...}` |
| `disconn` | Disconnect |
| `all` | Refresh hardware stats (`PcInfo` JSON) |

`GET` / `DELETE` / `STATE` are acknowledged with `200`.

## 5. ScreenConfig (display-mode descriptor)

```jsonc
{
  "Type": "image",              // informational (unused by the renderer)
  "id": "Customization",        // "Customization" = user media; else a preset
  "screenMode": "Full Screen",  // "Full Screen" | "Screen Splitting"
  "ratio": "1:1",               // "" | "1:1" | "3:2" | "4:3" | "16:9"
  "playMode": "Single",         // "Single" | "Loop" | "Shuffle"
  "media": ["a.jpg", "b.mp4"],  // file names relative to sdcard/pcMedia/
  "sysinfoDisplay": [],         // overlay stat names (or [] to hide)
  "settings": {                 // PmSetting overlay (badges must be a list)
    "align": "Center",          // "Left" | "Center" | "Right"
    "color": "#FFFFFF",
    "badges": [],               // e.g. ["CPU Badge", "GPU Badge"]
    "position": "Top",          // "Top" | "Bottom" (waterfall)
    "filter": {"value": "", "opacity": 0}   // "" = no rain/smoke animation
  }
}
```

### Display semantics (`MainActivity.handleMessage`, MSG 105)

* `id == "Customization"` → show the `media` list:
  * `screenMode == "Full Screen"` → one layout, sized by `ratio`.
  * `screenMode == "Screen Splitting"` → two layouts (`media[0]`, `media[1]`),
    `playMode` forced to `single`.
* `id != "Customization"` → preset theme: plays
  `/system/media/video/<id.split(":")[1]>.mp4` (e.g. `id="Pre-set 4: Exo-Ecologies"`).

### Media playback (`MSG_HANDEL_MEDIA`, MSG 102)

* `playMode == "Single"` → show `media[0]` (loop).
* `playMode == "Loop"` → cycle `media[showNum++]` (wrap around).
* `playMode == "Shuffle"` → random index.
* A path ending in `.mp4` plays via `VideoView`; anything else loads via
  Glide (image), advancing after 5 s when the play mode isn't single.

> ⚠️ Case matters: the `MSG_HANDEL_MEDIA` dispatcher compares against the
> **capitalised** `"Single"` / `"Loop"` / `"Shuffle"`, while the per-item
> player helper `setLayout1Path` checks lowercase `"single"`. Send the
> capitalised forms; with `"Single"` a video still loops (via its completion
> callback re-triggering `MSG_HANDEL_MEDIA`), so it behaves identically.

## 6. Changing the image/video (host procedure)

1. Upload file to `sdcard/pcMedia/<name>`:
   * **Preferred:** `adb push <file> /sdcard/pcMedia/<name>` (fast), or
   * Serial: `POST transport` → raw bytes → `POST transported`.
2. Send the switch command:

```
POST waterBlockScreenId 1
SeqNumber=N
ContentType=json
ContentLength=<len>

{"Type":"image","id":"Customization","screenMode":"Full Screen",
 "ratio":"1:1","playMode":"Single","media":["<name>"],
 "sysinfoDisplay":[],
 "settings":{"align":"Center","color":"#FFFFFF","badges":[],
             "position":"Top","filter":{"value":"","opacity":0}}}
```

Use `playMode:"Single"` for one image/video, `"Loop"` for a slideshow.

## 7. Real KANALI traffic (ground truth from device logs)

```
POST config 1
{"temperature":"Celsius",
 "waterBlockScreen":{"enable":true,"displayInSleep":true,
   "waterfallMode":false,"brightness":29,"rotate":270,
   "id":{"id":"Pre-set 4: Exo-Ecologies",
         "settings":{"color":"#dcdcdc","align":"Left",
           "filter":{"value":null,"opacity":100},
           "badges":["CPU Badge","GPU Badge"],"position":"Top"},
         "sysinfoDisplay":["CPU Temperature","GPU Temperature"]},
   "fanLCD":{"mode":"Smart Mode",
     "smartMode":[[0,10],[10,20],[30,30],[50,40],[65,55],[80,70],[90,100],[100,100]],
     "fixedMode":40}},
 "spec":{"cpu":"<your CPU>","gpu":"<your GPU>"}}
```

## 8. Implementation

The `tryx` package (`protocol.py`, `device.py`, `cli.py`, `gui.py`) implements
this protocol. See the project README for usage.
