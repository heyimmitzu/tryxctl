# TRYX Panorama 240 SE (cm01_se) — Reverse-engineering notes

## Device identity
- Rockchip Android 11 board, brand `TRYX`, device/product `cm01_se`.
- USB: `18d1:2d04` (Google "accessory+audio" VID/PID reused by Rockchip gadget).
- 3 interfaces: CDC ACM (serial console/control), CDC Data, ADB Interface.
- Config name `acm_adb`. Host sees `/dev/ttyACM0` for the CDC ACM serial channel.

## Apps
- `com.baiyi.homeui.tkcfanhomeui` (HomeUI) — the actual display UI (launcher/HOME).
- `com.baiyi.service.serialservice.serialdataservice` (SerialService) — owns the serial channel.
- `com.baiyi.app.factorymode` — factory mode.
- OEM codename `baiyi` (百翼). Product id reported as `cm01`.

## Communication
Host PC <-> ttyACM0 (USB CDC ACM) <-> /dev/ttyGS0 (gadget) <-> SerialService <-> HomeUI.

Serial baud: 115200. SerialService opens `/dev/ttyGS0`.

ADB also available (interface 2) when device enumerated.

## Serial wire framing (from SerialService `SerialMsgManager.sendRequestMsg` + `DataConvert`)
Frame = `0x5A` + stuffed_payload + `0x5A`

payload (before stuffing) = [len_hi len_lo][content_bytes][crc]

- `len` = 2 bytes big-endian = content_length + 5
- `content` = UTF-8 bytes of the HTTP-like message (below)
- `crc` = 1 byte = (sum of [len_hi len_lo content_bytes]) & 0xFF  (simple 8-bit checksum)

Byte stuffing (applied to len+content+crc, between the 0x5A delimiters):
- 0x5A -> 0x5B 0x01
- 0x5B -> 0x5B 0x02

Receive side un-stuffs `5B 01 -> 5A`, `5B 02 -> 5B`, then discards CRC (not verified).

## Message content (HTTP-like, UTF-8)
```
<METHOD> <CMD> <VERSION>\r\n
HeaderKey=Value\r\n
...            \r\n
\r\n
<body>
```
- METHOD ∈ {GET, POST, STATE, DELETE}
- VERSION = "1"
- Headers are DataHeader fields serialized as key=value where value != "-1".
  DataHeader fields: SeqNumber, AckNumber, ContentLength, ContentType, FileName,
  FileSize, ContentRange, Counter, Date, msgId.

Example (handshake request):
```
POST conn 1\r\nContentType=json\r\nContentLength=0\r\nAckNumber=0\r\n\r\n
```

## Commands handled by SerialService (`SerialMsgReceiverHandler`) — host -> device
- `POST conn` — handshake. Replies JSON:
  `{"attribute":["Status","Water Block Screen","Fan LCD|rw",("Turbo Pump")],"OS":"Android","productId":"cm01","version":{"app":"...","firmware":"...","hardware":"..."},"sn":"..."}`
- `POST transport` — begin file transfer. Body: `{"fileName":"x","fileSize":N,"type":"<use>"}`.
  Opens `/sdcard/pcMedia/<fileName>` (note: relative path built from `sdcard/pcMedia/`).
  Replies `{"state":"success","blockMaxSize":888888888}`.
- RAW bytes (unframed) — after transport, device writes incoming raw bytes to the open
  file until `fileSize` reached, then acks.
- `POST transported` — end transfer. Body: `{"fileName":"x","md5":"..."}`.
  If type == "firmware" -> triggers RK update broadcast.
- `POST mediaDelete` — body `{"include":["f1","f2"]}` or `{"exclude":["f1"]}`. Deletes in /sdcard/pcMedia/.
- `POST turboPump` — body `{"enable":bool,"value":int}`. Writes i2c aio_cooler pwm/control_source.
- `POST config` (turboPump variant) — body `{"turboPump":{"enable":bool,"value":int}}`.
- `GET` / `DELETE` / `STATE` — just ack `200`.

## Commands handled by HomeUI (`MsgReceiverManager`) — host -> device (display control)
All are `POST <cmd> 1` with JSON body.
- `sysinfoDisplay` — `{"items":["Cpu Usage",...]}` (which stats to show).
- `config` — MAIN CONFIG. Body:
  ```
  {
    "temperature": "celsius"|"fahrenheit",
    "waterBlockScreen": {
      "enable": bool,
      "displayInSleep": bool,
      "brightness": int,          // 0..100 -> *2.5
      "id": { <ScreenConfig> },
      "waterfallMode": bool,
      "fanLCD": { ... }
    },
    "spec": {"cpu":"...","gpu":"..."}
  }
  ```
- `waterBlockScreenId` — body is a `ScreenConfig` (switch water-block screen directly).
- `preset` — body is a `ScreenConfig`.
- `rotate` — `{"degree":N}`.
- `displayInSleep` — `{"enable":bool}`.
- `waterfallMode` — `{"enable":bool}`.
- `all` / STATE `all` — refresh with `PcInfo` JSON.
- `spec` — `{"cpu":"...","gpu":"..."}`.
- `power` — `{"event":"..."}`.
- `temperature` — `{"value":"celsius"|"fahrenheit"}`.
- `brightness` — `{"value":int}`.
- `waterBlockScreen` — `{"enable":bool}`.
- `disconn` — disconnect.

## ScreenConfig (display mode descriptor)
```java
class ScreenConfig {
  String Type;            // e.g. "image"|"video"|"sysinfo"|...  (TBD exact values)
  String id;              // screen id
  String screenMode;      // e.g. "Screen Splitting"
  String ratio;           // aspect ratio
  String playMode;        // "single" | (cycle)
  ArrayList<String> media;  // media file paths
  Object sysinfoDisplay;
  Object settings;
}
```

## Media display (HomeUI `MainActivity`)
- `basePath = "sdcard/pcMedia/"` (relative path).
- `basevideoPath = "/system/media/video/"`, `defaultPath = "/system/media/video/default.mp4"`,
  `stanbyPath = "/system/media/video/standby.mp4"`.
- Bundled theme videos live in the APK res/ (huge .webp files = animated themes).
- Animations: `/system/media/anim/*.webp` (rain.webp, vapor.webp).
- `setLayout1Path(str)` / `setLayout2Path(str)`:
  - if file missing -> advance to next media (MSG 102).
  - if path ends with `mp4` -> VideoView loop (loop if playMode empty or "single").
  - else -> Glide image load (jpg/png/etc.); if playMode != "single", advance after 5000ms.
- Two layouts (layout1 / layout2) support "Screen Splitting" mode.
- Fan speed sysfs: `/sys/bus/platform/drivers/lcd_fan/speed`.
- Pump control sysfs: `/sys/bus/i2c/drivers/aio_cooler/{control_source,pwm,rpm}`.

## TODO (delegate to worker)
- Recover `MainActivity$1.handleMessage` (4508 instructions, jadx failed) via `--show-bad-code`
  or smali, to get exact handling of MSG_REFRESH_CONFIG(105) and MSG_HANDEL_MEDIA(102):
  - exact `Type` / `id` / `screenMode` string values and branches;
  - how `media` paths are resolved (relative vs absolute, basePath prefix);
  - exact JSON to set a single image, a video, a slideshow, and the sysinfo screen.
- Confirm screen resolution and supported media formats.
