# Fuck VIDAA

> ⚠️ **No interactive launchers yet.** On current VIDAA firmware (2024+) you can
> **control** your TV and **render** a custom home screen on it, but you **cannot
> navigate/drive that screen** — the firmware blocks both the `hisense://debug`
> app-install *and* passing remote input to a web page. See
> [What works / what doesn't](#what-works--what-doesnt). This repo is a
> control + reverse-engineering toolkit, not a finished launcher.

Tools to control a **Hisense VIDAA TV** over its RemoteNow MQTT protocol — including
the **modern 2024+ firmware** that rejects every older open-source project with
`CONNACK 5 (Not authorized)`. For *your own* TV, on your own LAN.

This started as a fork of [Zfinix/vidaa-launcher](https://github.com/Zfinix/vidaa-launcher)
and turned into cracking the modern auth stack end-to-end.

---

## What we figured out (the hard part)

Older VIDAA control libraries break on 2024+ TVs. Here's the full chain we reverse-engineered
and got working on real hardware (two Hisense A6-series, `transport_protocol 3290`):

1. **The cert wall.** Modern firmware enforces the client-certificate identity
   `CN=VidaaAppAndroidV01` and rejects the old 2018 `remoteclientmobile` cert (the one
   every old project ships) with `CONNACK 5`. The modern cert lives inside the official
   **VIDAA Smart TV** app (`com.universal.remote.multi`) at `res/raw/client_mobile_android.p12`.
2. **Auto-detection.** MAC, `transport_protocol`, and brand are read, unauthenticated, from the
   TV's UPnP descriptor (`http://<TV_IP>:18400/MediaServer/rendererdevicedesc.xml`).
3. **Modern pairing.** `vidaa_app_connect` → TV shows a 4-digit PIN → `authenticationcode`
   (`authNum` as an **int**) → `platform_service/data/gettoken` (note: **`data/`**, not
   `actions/`) → the TV issues an **access token**.
4. **Token auth.** All topics key off the full **`client_id`** (not `<MAC>$normal`). Persistent
   sessions reconnect with the stored `client_id` + stored pairing `username` + **access token as
   the MQTT password**.
5. **Reads are UPnP, not MQTT.** Modern MQTT is commands-only. Volume / mute / playback come from
   UPnP SOAP (RenderingControl / AVTransport on port 18400). `gettvstate` over MQTT is **not** a
   query — it *triggers the pairing PIN*, so never poll it.

## What works / what doesn't

**✅ Works (verified on real TVs):**
- Connect + pair on modern firmware; persistent token auth
- **Full remote control** over MQTT: any key (`sendkey`), volume, power, navigation, app-launch
  command, source-change command
- **Live state via UPnP**: volume, mute, playback/transport state
- **Rendering** the custom launcher page on the TV via a URL-launch
  (`launchapp` with `urlType: 37`)

**❌ Blocked by the firmware (not a bug — Hisense locked these down):**
- **Interactive launcher** — `hisense://debug` app-install is refused by the TV browser, and the
  browser passes **no d-pad input** (no key events, no spatial focus) to a web page. So the
  launcher displays but can't be navigated/selected.
- **App list / source list** — the TV never returns these over MQTT (or UPnP), so you can't
  enumerate installed apps or reliably launch store apps by tile.

## Requirements

- A Hisense VIDAA TV on your LAN (tested on `transport_protocol 3290`, 2024 models)
- Python 3 + `paho-mqtt` v2 (`pip install paho-mqtt`), plus `openssl`
- The modern client certificate (see below) — **not included; extract it from your own app install**

## Getting the certificate

The modern cert is not redistributed by any project. Extract it from the official **VIDAA Smart TV**
app (package `com.universal.remote.multi`) — an APK is just a zip:

```sh
unzip -j vidaa_smart_tv.apk res/raw/client_mobile_android.p12 -d .
# the keystore password is the community-documented value used by hisense_vidaa:
openssl pkcs12 -legacy -in client_mobile_android.p12 -clcerts -nokeys -out server/keys/vidaa_client.pem -passin pass:186e990688070325a1c4b0ce275d2388
openssl pkcs12 -legacy -in client_mobile_android.p12 -nocerts -nodes  -out server/keys/vidaa_client.key -passin pass:186e990688070325a1c4b0ce275d2388
```

Confirm the subject is `CN=VidaaAppAndroidV01`. The code auto-detects
`server/keys/vidaa_client.pem` + `.key` (or set `HISENSE_CERT` / `HISENSE_KEY`).

## Usage

```sh
# 1. pair (TV must be ON; read the 4-digit PIN off the screen)
HISENSE_HOST=<TV_IP> python server/pair_modern.py
echo <pin> > server/.paircode          # the script submits it and saves a token

# 2. run the control server
echo <TV_IP> > server/.host
python server/tvsite_server.py launcher 8765

# 3. control the TV over HTTP
curl "http://<SERVER_IP>:8765/api/key?k=KEY_VOLUMEUP"
curl "http://<SERVER_IP>:8765/api/key?k=KEY_MUTE"
curl "http://<SERVER_IP>:8765/api/volume?v=15"
```

Tokens are saved per-TV in `server/keys/.token_<TV_IP>.json` (access token ~2 days,
refresh token ~30 days; auto-reload is implemented, refresh is not).

Read live volume over UPnP (no cert needed):

```sh
# SOAP GetVolume to RenderingControl at http://<TV_IP>:18400/upnp/control/mingusrcr
```

## Files

```
server/tv.py             modern VIDAA client: UPnP auto-detect, cert resolve, token auth
server/pair_modern.py    modern pairing flow (vidaa_app_connect -> PIN -> token)
server/tvsite_server.py  HTTP server + /api/key, /api/volume, serves the launcher page
server/backend_rpc.py    single shared TV connection, JSON command bridge
server/discover.py       SSDP discovery
launcher/                the canvas home-screen page (renders on the TV; not navigable yet)
```

## Credits & license

- Based on [Zfinix/vidaa-launcher](https://github.com/Zfinix/vidaa-launcher) (MIT).
- Protocol reverse-engineering cross-checked against
  [stevene1919/hisense_vidaa](https://github.com/stevene1919/hisense_vidaa) and
  [warrenrees/pyvidaa](https://github.com/warrenrees/pyvidaa).
- MIT. Not affiliated with Hisense or VIDAA. The certificate belongs to its owner and is **not**
  included here — bring your own, extracted from your own app install, for your own TV.
```
