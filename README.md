<h1 align="center">Fuck VIDAA</h1>

**Remote control, modern pairing, and an unfinished escape from the stock home screen.**

> [!WARNING]
> **No interactive launchers yet.** The custom page can be displayed on the tested TVs, but remote navigation did not reach the page and `hisense://debug` installation was rejected. This is an experimental TV-control and reverse-engineering toolkit. It does **not** replace VIDAA Home.

[![Status: experimental](https://img.shields.io/badge/status-experimental-orange)](#current-status)
[![Protocol: MQTT](https://img.shields.io/badge/protocol-MQTT-2563eb)](#under-the-hood)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)

[Status](#current-status) · [Get started](#get-started) · [HTTP API](#http-api) · [Research](#research-notes) · [Troubleshooting](#troubleshooting)

VIDAA will let a TV display a custom home screen, then make the useful part—controlling it—another problem entirely.

This project started from [Zfinix/vidaa-launcher](https://github.com/Zfinix/vidaa-launcher). Work here focused on connecting to newer TVs, pairing through their PIN flow, saving access tokens, and sending remote commands from a local server.

**The practical result today:** a Python control backend you can call from your PC, scripts, or another device on your LAN. The on-TV launcher remains a visual prototype.

## The home-screen experiment

![Launcher interface preview](docs/screenshots/home.png)

*Included launcher preview. This shows the interface design, not a working replacement home screen on the tested firmware.*

<details>
<summary>View the included interface demo</summary>

![Launcher interface animation](docs/screenshots/demo.gif)

*An interface demo does not establish that TV-remote navigation, app launching, or home takeover works on a particular television.*

</details>

## Current status

Development notes report testing on **two Hisense A6-series TVs** with **`transport_protocol 3290`**. These findings describe that setup; other models, regions, firmware, and app versions may behave differently.

| Capability | Status | What that means |
| --- | --- | --- |
| Modern certificate selection and authentication | Implemented; reported working | You supply the compatible client certificate and private key. |
| PIN pairing and saved token authentication | Implemented; reported working | Reuse a valid per-TV token with the original client ID and username. |
| Remote keys and volume commands | Implemented; reported working | Send commands through Python or HTTP. Bridge success is not a TV-side acknowledgment. |
| TV discovery and device information | Implemented | SSDP and the UPnP descriptor identify the host, MAC, brand, and authentication mode. |
| Showing the custom page on the TV | Observed during experiments | A `launchapp` payload with `urlType: 37` displayed the page. No dedicated URL-launch HTTP endpoint exists. |
| Navigating the page with the TV remote | Not working on the tested firmware | Instrumented d-pad tests received neither key events nor spatial-focus movement. |
| `hisense://debug` installation | Rejected on the tested firmware | It is not a working setup step for these TVs. |
| App/source lists and tile launching | Unreliable on the tested firmware | Commands exist, but lists did not arrive. Name-based launching depends on those lists. |
| UPnP volume/mute/playback readouts | Reported in experiments; not shipped | Descriptor discovery exists; the committed server has no UPnP SOAP state reader. |
| Automatic token refresh | Not implemented | Expired access tokens currently require pairing again. |
| Automatic home takeover | Unfinished | The legacy watcher is not a supported solution for the tested modern firmware. |

A missing app-list response does not mean your TV has no apps. A page appearing on-screen does not mean its remote controls work. Those are separate parts of the project.

## Get started

You need a VIDAA TV on the same network, Git, Python with `pip`, and a compatible client certificate/private-key pair. The dependency is **`paho-mqtt` 2.x**. Certificate extraction also requires OpenSSL.

Run commands from the repository root. **`192.168.1.50` is an example address; replace it with your TV's IP.** Use `python3` instead of `python` if that is your Python command.

### 1. Clone and install

```sh
git clone https://github.com/preinfection/vidaa-control.git
cd vidaa-control
python -m pip install "paho-mqtt>=2,<3"
python -c "from pathlib import Path; Path('server/keys').mkdir(parents=True, exist_ok=True)"
```

### 2. Supply the certificate

The repository does **not** include the certificate, private key, or your pairing tokens. Place a compatible pair here:

```text
server/keys/vidaa_client.pem
server/keys/vidaa_client.key
```

The modern identity used during testing was **`CN=VidaaAppAndroidV01`**. The older `remoteclientmobile` certificate was rejected on those TVs. An arbitrary certificate with the same subject name is not sufficient.

Alternatively, set both `HISENSE_CERT` and `HISENSE_KEY` to your certificate/key paths before starting Python.

<details>
<summary>Extracting the certificate from your own VIDAA app package</summary>

The Android package used during the project was **VIDAA Smart TV** (`com.universal.remote.multi`). It contained `res/raw/client_mobile_android.p12`. Packaging, certificates, and keystore credentials can differ between releases.

With your own APK available as `vidaa_smart_tv.apk`, extract it into the ignored keys directory:

```sh
python -m zipfile -e vidaa_smart_tv.apk server/keys/apk
```

The package used in the original experiments used the keystore password below. This is not your TV PIN or a personal access token:

```sh
openssl pkcs12 -legacy -in server/keys/apk/res/raw/client_mobile_android.p12 -clcerts -nokeys -out server/keys/vidaa_client.pem -passin pass:186e990688070325a1c4b0ce275d2388
openssl pkcs12 -legacy -in server/keys/apk/res/raw/client_mobile_android.p12 -nocerts -nodes -out server/keys/vidaa_client.key -passin pass:186e990688070325a1c4b0ce275d2388
openssl x509 -in server/keys/vidaa_client.pem -noout -subject
```

These commands require OpenSSL's legacy PKCS#12 support. If a newer package has different contents or credentials, this recipe may not apply. Keep the APK outside the repository and do not commit extracted keys.

</details>

### 3. Pair — use two terminals

Turn the TV fully on and keep its screen visible. The script waits for a PIN file; it does **not** currently offer an interactive PIN prompt.

**Terminal A:** start pairing.

Windows PowerShell:

```powershell
$env:HISENSE_HOST = "192.168.1.50"
python server/pair_modern.py
```

macOS/Linux:

```sh
HISENSE_HOST=192.168.1.50 python server/pair_modern.py
```

**Terminal B:** open another terminal in the same repository root. After the TV displays its PIN, write that code. Replace `1234` with the real PIN.

Windows PowerShell:

```powershell
Set-Content -Path server/.paircode -Value "1234" -Encoding ascii
```

macOS/Linux:

```sh
printf '%s\n' '1234' > server/.paircode
```

Write the file **after** starting Terminal A: the script deletes an old `.paircode` at startup. It waits up to ten minutes. Re-running it also removes that TV's old token before pairing again.

Look for **`PIN ACCEPTED.`** followed by **`TOKEN SAVED`**. The script then performs optional list/reconnect probes. Missing replies to those probes do not by themselves establish that remote commands are unauthorized.

> [!NOTE]
> Pairing diagnostics print raw responses that can contain tokens, device identifiers, and the PIN. Redact them before sharing logs or opening an issue.

### 4. Start the control server

After pairing finishes:

```sh
python server/tvsite_server.py launcher 8765
```

The pairing connection remembers the TV in `server/.host`. The server prefers a reachable cached host, then SSDP discovery, then the `HISENSE_HOST` fallback. It controls **one selected TV at a time**, while storing tokens separately per TV.

<details>
<summary>Switch to another paired TV</summary>

Pair that TV first, update the cached address, and restart the server. Setting `HISENSE_HOST` alone does not override a reachable cached TV.

Windows PowerShell:

```powershell
Set-Content -Path server/.host -Value "192.168.1.50" -Encoding ascii
```

macOS/Linux:

```sh
printf '%s\n' '192.168.1.50' > server/.host
```

</details>

### 5. Try a command

From the server computer:

```sh
curl "http://127.0.0.1:8765/api/key?k=KEY_VOLUMEUP"
curl "http://127.0.0.1:8765/api/key?k=KEY_MUTE"
curl "http://127.0.0.1:8765/api/volume?v=15"
```

In Windows PowerShell, use **`curl.exe`** if `curl` resolves to a PowerShell alias. From another device, replace `127.0.0.1` with the server computer's LAN address. Keep the server computer awake.

A successful bridge response looks like:

```json
{"ok": true}
```

Confirm the action on the TV. The response means the bridge accepted/published the command, not that the TV acknowledged performing it.

Open `http://127.0.0.1:8765/` for a desktop-browser preview. This does not install the launcher on the TV, replace Home, or resolve the TV's remote-input limitation.

> [!CAUTION]
> The HTTP server binds to `0.0.0.0`, has no authentication, and exposes GET routes that change TV state. Keep it on a trusted LAN; do not expose port `8765` to the internet. The MQTT client also disables server-certificate verification. This is not a hardened remote-access service.

## HTTP API

All routes use **GET** and return JSON. Inspect `ok`, `error`, and `offline`; HTTP status alone does not describe command success.

| Route | Purpose | Limitation |
| --- | --- | --- |
| `/api/key?k=KEY_VOLUMEUP` | Send a remote key | Requires a reachable, authorized TV. |
| `/api/volume?v=15` | Set volume | Numeric values are clamped to `0–100`. |
| `/api/state` | Return the last cached MQTT state broadcast | Can return `null`; it does not actively query UPnP. |
| `/api/apps` | Request and filter app names | Can time out when firmware does not return a list. |
| `/api/launch?name=Netflix` | Find and launch an app by name | Depends on a working app list. |
| `/api/sources` | Request source/input information | Can time out on the tested firmware. |
| `/api/source?id=HDMI1` | Find and select a source | Needs an actual returned ID/name; `HDMI1` is illustrative. |

No dedicated HTTP route exists for arbitrary URL launching, token refresh, or UPnP SOAP readouts.

## Under the hood

```text
PC / phone / local script
          |
          | HTTP :8765
          v
  tvsite_server.py
          |
          v
    backend_rpc.py
          |
          | TLS MQTT :36669
          v
    Hisense VIDAA TV
```

`tv.py` also reads the UPnP device descriptor, trying ports `18400` and `38400`. Descriptor discovery is separate from SOAP volume/playback queries, which are not implemented here yet.

### Connection settings

Set these environment variables before starting Python. Restart the server after changing them.

| Variable | Purpose |
| --- | --- |
| `HISENSE_HOST` | Explicit host for modern pairing; a fallback after the cached host and discovery when the control server selects a TV. |
| `HISENSE_CERT` | Path to your client certificate. Set together with `HISENSE_KEY`. |
| `HISENSE_KEY` | Path to the matching client private key. |
| `HISENSE_AUTH` | Optional authentication-mode override: `static`, `middle`, or `modern`. Normally selected from the device descriptor. |
| `HISENSE_MAC` | Optional override when descriptor lookup cannot supply the TV's real MAC address. |

### Research notes

The main progress was getting through the authentication chain on the tested newer firmware:

1. **Certificate identity:** the tested TVs rejected the older certificate and accepted the modern client identity used by the project.
2. **Device information:** the UPnP descriptor provides the MAC, brand, and `transport_protocol` for authentication-mode selection.
3. **PIN handshake:** `vidaa_app_connect` requests pairing; `authenticationcode` submits `authNum` as an integer.
4. **Token request:** `/remoteapp/tv/platform_service/<client_id>/data/gettoken` uses **`data/`**, rather than the normal **`actions/`** path.
5. **Reconnect:** a valid token reuses the original full `client_id` and pairing `username`, with the access token as the MQTT password.
6. **On-TV rendering:** a URL-launch experiment using `urlType: 37` displayed the custom page. Installation and remote input remained unresolved.

Tokens are saved in `server/keys/.token_<TV_IP>.json`. Validity is checked against the timestamps/durations returned by the TV. Valid tokens are loaded when a connection is created; automatic refresh and in-session renewal are not implemented.

> [!IMPORTANT]
> **Do not poll `gettvstate` as a modern-firmware state reader.** Project testing found it could trigger pairing instead. The diagnostic pairing script still probes it near the end, and the legacy `home_watch.py` polls it. That watcher is not part of the setup above.

## What still needs work

- **Interactive launcher:** a reproducible installation or remote-input path on affected firmware.
- **App/source discovery:** reliable enumeration, or a documented alternative that avoids missing list responses.
- **UPnP readouts:** bring the reported volume/mute/playback experiments into a maintained client and HTTP API.
- **Token lifecycle:** refresh, expiry recovery, and clearer re-pairing behavior.
- **Pairing UX:** direct PIN entry, redacted logs, and a clearer distinction between pairing success and optional probes.
- **Compatibility:** model, region, firmware, and `transport_protocol` reports for more TVs.

These experiments do not establish that interactive launchers are impossible on every VIDAA device. They establish that the attempted paths did not work on the TVs tested here.

## Troubleshooting

| Symptom | Check first |
| --- | --- |
| `CONNACK 5` or connection timeout | Correct host, TV fully on, compatible certificate, and correct MAC/auth mode from discovery. Not every timeout is a certificate failure. |
| No pairing PIN | Start `pair_modern.py` with an explicit `HISENSE_HOST`. `pair_daemon.py` uses the older trigger. |
| Still waiting for the PIN | Write `.paircode` after the script starts, from the repository root. Use ASCII in Windows PowerShell. |
| Wrong TV responds | Update `server/.host` and restart. A reachable cached TV takes precedence over the environment fallback. |
| Previously working control stops | Restart with a valid token or pair again. Automatic refresh is not implemented. |
| `/api/state` is `null` | No usable state broadcast is cached. This endpoint has no active UPnP state reader. |
| Apps/sources time out | This matches the tested limitation. Try a remote key separately to distinguish it from a connection problem. |
| Page displays; d-pad does nothing | This is the unresolved remote-input problem, not a completed interactive installation. |
| `hisense://debug` is rejected | Also observed on the tested TVs; it is not required for the control backend. |

## Repository map

```text
server/
  tv.py                 MQTT client, discovery, certificates, token loading
  pair_modern.py        Modern PIN/token pairing and diagnostic probes
  pair_daemon.py        Older pairing helper
  backend_rpc.py        Shared TV connection and JSON command bridge
  tvsite_server.py      Static hosting and HTTP control routes
  discover.py           SSDP discovery
  home_watch.py         Legacy takeover experiment; excluded from quick start
  keys/                 Local certificates/tokens; ignored by Git
launcher/
  launcher-canvas.html   Canvas launcher prototype
  launcher-canvas.src.html
  bg/                   Wallpaper assets and attribution
  icons/                App artwork
docs/screenshots/       Included interface preview and animation
```

## Contributing

Include your TV model, region, firmware version, and `transport_protocol` in a report. Describe connection, pairing, remote keys, app lists, page rendering, and remote input separately.

Never attach private keys, token files, raw pairing logs, or personal device identifiers. Check staged files before committing. The repository ignores `server/keys/` and the local host/PIN files.

## Credits and license

- Launcher foundation: [Zfinix/vidaa-launcher](https://github.com/Zfinix/vidaa-launcher).
- Protocol research cross-checked against [stevene1919/hisense_vidaa](https://github.com/stevene1919/hisense_vidaa) and [warrenrees/pyvidaa](https://github.com/warrenrees/pyvidaa).
- Wallpaper attribution and individual licenses: [launcher/bg/credits.json](launcher/bg/credits.json). Preserve applicable credits when reusing those assets.
- App names, logos, and certificates belong to their respective owners.

Project code is available under the [MIT license](LICENSE). Independent project; not affiliated with Hisense or VIDAA.
