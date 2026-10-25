import hashlib
import json
import os
import queue
import ssl
import threading
import time
import uuid

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = os.path.join(HERE, "keys")


def _resolve_cert():
    """Pick the client cert/key pair.

    Newer VIDAA firmware (2024+, protocol >= ~3290) enforces the modern
    certificate subject (CN=VidaaAppAndroidV01) and rejects the old 2018
    RemoteNOW cert (CN=remoteclientmobile) with CONNACK 5. So prefer, in order:
      1. HISENSE_CERT / HISENSE_KEY env paths
      2. a modern pair dropped in keys/ (vidaa_client.pem/.key or
         vidaa_2024_cert.pem + vidaa_2024_key.pem)
      3. the legacy keys/rcm_certchain_pem.cer + rcm_pem_privkey.pkcs8
    """
    env_c, env_k = os.environ.get("HISENSE_CERT"), os.environ.get("HISENSE_KEY")
    if env_c and env_k:
        return env_c, env_k
    modern = [
        ("vidaa_client.pem", "vidaa_client.key"),
        ("vidaa_2024_cert.pem", "vidaa_2024_key.pem"),
        ("vidaa_client_v02.pem", "vidaa_client_v02.key"),
    ]
    for c, k in modern:
        cp, kp = os.path.join(KEYS, c), os.path.join(KEYS, k)
        if os.path.exists(cp) and os.path.exists(kp):
            return cp, kp
    return (os.path.join(KEYS, "rcm_certchain_pem.cer"),
            os.path.join(KEYS, "rcm_pem_privkey.pkcs8"))


CERT, KEY = _resolve_cert()


def _load_token(host):
    try:
        with open(_token_path(host)) as f:
            return json.load(f)
    except Exception:
        return None


def _token_valid(tok):
    """True if a saved access token is present, has its pairing username, and
    is still inside its validity window (minus a 5-min safety margin)."""
    if not (tok and tok.get("accesstoken") and tok.get("username")):
        return False
    try:
        t0 = int(tok.get("accesstoken_time", 0))
        days = int(tok.get("accesstoken_duration_day", 0))
        return time.time() < t0 + days * 86400 - 300
    except (TypeError, ValueError):
        return False

DEFAULT_HOST = os.environ.get("HISENSE_HOST")
DEFAULT_PORT = 36669
HOST_CACHE = os.path.join(HERE, ".host")

# Newer VIDAA firmware (transport_protocol >= 3000) rejects the fixed
# hisenseservice login with CONNACK 5 and wants per-connection credentials.
# Algorithm from pyvidaa (github.com/warrenrees/pyvidaa).
#
# The dynamic credential schemes embed the TV's MAC in the client_id; the TV
# recomputes that hash from its own MAC, so a placeholder MAC is rejected with
# CONNACK 5 even when the mode is right. MAC + protocol version + brand are
# read, unauthenticated, from the TV's UPnP descriptor (same source pyvidaa
# uses). Overrides: HISENSE_AUTH=static|middle|modern, HISENSE_MAC=aa:bb:...
# Mode auto-selects from transport_protocol: <3000 static, 3000-3289 middle,
# >=3290 modern.
AUTH_MODE = os.environ.get("HISENSE_AUTH")  # None => auto-select from protocol
HISENSE_MAC = os.environ.get("HISENSE_MAC")
UPNP_PORTS = (18400, 38400)
_DEVICE_INFO_CACHE = {}


def _token_path(host):
    """Per-TV token file, so multiple paired TVs don't clobber each other."""
    return os.path.join(KEYS, f".token_{host}.json")
_PATTERN = "38D65DC30F45109A369A86FCE866A85B"
_SUFFIX_MODERN = "h!i@s#$v%i^d&a*a"
_SUFFIX_MIDDLE = "h*i&s%e!r^v0i1c9"
_XOR = 0x5698_1477_2b03_a968


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def dynamic_credentials(mac, mode="modern", brand="his"):
    """Return (client_id, username, password) for a fresh connection."""
    ts = int(time.time())
    client_id = f"{mac}${brand}${_md5(f'{_PATTERN}${mac}')[:6]}_vidaacommon_001"
    username = f"{brand}${ts ^ _XOR}"
    suffix = _SUFFIX_MODERN if mode == "modern" else _SUFFIX_MIDDLE
    remainder = sum(int(d) for d in str(ts)) % 10
    password = _md5(f"{ts}${_md5(f'{brand}{remainder}{suffix}')[:6]}")
    return client_id, username, password


def _cached_host():
    try:
        with open(HOST_CACHE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _remember_host(host):
    try:
        with open(HOST_CACHE, "w") as f:
            f.write(host)
    except OSError:
        pass


def _reachable(host, port, timeout=0.8):
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_host(port=DEFAULT_PORT):
    """Last paired host first, then SSDP, then the static default."""
    cached = _cached_host()
    if cached and _reachable(cached, port):
        return cached
    try:
        from discover import discover

        found = discover()
    except Exception:
        found = None
    if found and _reachable(found, port):
        return found
    if DEFAULT_HOST and _reachable(DEFAULT_HOST, port):
        return DEFAULT_HOST
    raise OSError("TV not reachable")


def _norm_mac(mac):
    """Normalise a MAC to colon-separated lowercase (the form the TV hashes)."""
    if not mac:
        return mac
    h = mac.strip().replace(":", "").replace("-", "")
    if len(h) == 12 and all(c in "0123456789abcdefABCDEF" for c in h):
        h = h.lower()
        return ":".join(h[i:i + 2] for i in range(0, 12, 2))
    return mac.strip()


def _auto_mode(proto):
    """Map transport_protocol to an auth mode (pyvidaa thresholds)."""
    try:
        p = int(proto)
    except (TypeError, ValueError):
        return "modern"
    if p < 3000:
        return "static"
    if p < 3290:
        return "middle"
    return "modern"


def detect_device_info(host, ports=UPNP_PORTS, timeout=5.0):
    """Read mac / transport_protocol / brand from the TV's UPnP descriptor.

    Unauthenticated HTTP GET of /MediaServer/rendererdevicedesc.xml, the same
    source pyvidaa uses to pick an auth method. Returns {} if unreachable.
    """
    import re
    import urllib.request

    if host in _DEVICE_INFO_CACHE:
        return _DEVICE_INFO_CACHE[host]
    info = {}
    for port in ports:
        url = f"http://{host}:{port}/MediaServer/rendererdevicedesc.xml"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                body = r.read().decode("utf-8", "ignore")
        except Exception:
            continue
        m = re.search(r"transport_protocol[=:\s]*?(\d+)", body, re.I)
        if m:
            info["transport_protocol"] = int(m.group(1))
        m = re.search(r"(?mi)^\s*mac=([0-9a-fA-F:]{12,17})", body)
        if m:
            info["mac"] = m.group(1)
        m = re.search(r"(?mi)^\s*brand=(\w+)", body)
        if m:
            info["brand"] = m.group(1)
        break
    _DEVICE_INFO_CACHE[host] = info
    return info


class HisenseTV:
    """Minimal VIDAA MQTT remote client (paho-mqtt v2)."""

    def __init__(self, host=None, port=DEFAULT_PORT, timeout=12.0,
                 mac=None, mode=None):
        if host is None:
            host = resolve_host(port)
        self.host = host
        self.port = port
        self.timeout = timeout
        info = detect_device_info(host)
        mac = mac or HISENSE_MAC or info.get("mac") or "XX:XX:XX:XX:XX:XY"
        self._mac = _norm_mac(mac)
        self._device = f"{self._mac.upper()}$normal"
        self.brand = info.get("brand") or "his"
        self.mode = mode or AUTH_MODE or _auto_mode(info.get("transport_protocol"))
        self._q = queue.Queue()
        self.connected = threading.Event()
        self.muted = False
        self.volume = None
        self.state = None
        self.apps = None

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(certfile=CERT, keyfile=KEY)
        self._ctx = ctx

        if self.mode == "static":
            self.client_id = f"HisenseBar/{uuid.uuid4()}"
            user, pw = "hisenseservice", "multimqttservice"
        else:
            self.client_id, user, pw = dynamic_credentials(
                self._mac, mode=self.mode, brand=self.brand)
            # Modern/middle firmware keys every topic off the full client_id,
            # not the legacy "<MAC>$normal" device string.
            self._device = self.client_id

        # Once paired, connect with the saved access token: the EXACT client_id
        # and username from pairing, with the access token as the password.
        # Commands are authorized immediately (no vidaa_app_connect needed).
        self.using_token = False
        tok = _load_token(self.host)
        if self.mode != "static" and _token_valid(tok):
            self.client_id = tok.get("client_id", self.client_id)
            self._device = self.client_id
            user = tok["username"]
            pw = tok["accesstoken"]
            self.using_token = True

        self.username = user
        self.password = pw
        self._c = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id
        )
        self._c.username_pw_set(user, pw)
        self._c.tls_set_context(self._ctx)
        self._c.on_connect = self._on_connect
        self._c.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            self.connect_error = str(reason_code)
            return
        client.subscribe(f"/remoteapp/mobile/{self._device}/#")
        client.subscribe("/remoteapp/mobile/broadcast/#")
        self.connected.set()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload
        if payload:
            try:
                payload = json.loads(payload.decode("utf-8", "strict"))
            except (ValueError, UnicodeDecodeError):
                payload = msg.payload
        if isinstance(payload, dict):
            if msg.topic.endswith("/volumechange"):
                if payload.get("volume_type") == 2:
                    self.muted = bool(payload.get("volume_value"))
                elif payload.get("volume_type") == 0:
                    self.volume = int(payload.get("volume_value", 0))
            elif msg.topic.endswith("/ui_service/state") and "statetype" in payload:
                self.state = payload
        self._q.put((msg.topic, payload))

    def connect(self):
        self._c.connect(self.host, self.port)
        self._c.loop_start()
        if not self.connected.wait(self.timeout):
            raise TimeoutError("MQTT connect timed out")
        _remember_host(self.host)

    def disconnect(self):
        try:
            self._c.disconnect()
        finally:
            self._c.loop_stop()

    def _drain(self):
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def _wait(self, match=None):
        import time as _t
        deadline = _t.time() + self.timeout
        while _t.time() < deadline:
            try:
                item = self._q.get(block=True, timeout=max(0.1, deadline - _t.time()))
            except queue.Empty:
                break
            if match is None or match(*item):
                return item
        raise TimeoutError(f"no matching response in {self.timeout:.0f}s")

    def _call(self, service, action, payload=None):
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        topic = f"/remoteapp/tv/{service}/{self._device}/actions/{action}"
        self._c.publish(topic, payload).wait_for_publish()

    def start_authorization(self):
        """Trigger the TV to display the 4-digit pairing code."""
        self._drain()
        self._call("ui_service", "gettvstate")

    def send_authorization_code(self, code):
        self._drain()
        self._call("ui_service", "authenticationcode", {"authNum": str(code)})
        _, payload = self._wait()
        if isinstance(payload, dict) and int(payload.get("result", 0)) == 1:
            return True
        raise RuntimeError(f"authorization failed: {payload}")

    def send_key(self, key):
        self._call("remote_service", "sendkey", key)

    def set_volume(self, volume):
        volume = max(0, min(100, int(volume)))
        self._call("platform_service", "changevolume", str(volume))

    def source_list(self):
        self._drain()
        self._call("ui_service", "sourcelist")
        _, payload = self._wait(match=lambda t, p: isinstance(p, list))
        return payload

    def app_list(self):
        if self.apps is None:
            self._drain()
            self._call("ui_service", "applist")
            _, payload = self._wait(match=lambda t, p: isinstance(p, list))
            self.apps = [a for a in payload if isinstance(a, dict) and a.get("name")]
        return self.apps

    def find_app(self, name):
        want = name.strip().lower()
        apps = self.app_list()
        for a in apps:
            if a["name"].lower() == want:
                return a
        for a in apps:
            if want in a["name"].lower() or a["name"].lower() in want:
                return a
        return None

    def launch_app(self, name):
        app = self.find_app(name)
        if app is None:
            raise LookupError(f"app not installed: {name}")
        payload = {k: app[k] for k in ("appId", "name", "url", "urlType", "storeType") if k in app}
        self._call("ui_service", "launchapp", payload)
        return app

    def change_source(self, payload):
        self._call("ui_service", "changesource", payload)

    def get_volume(self):
        if self.volume is None:
            self._drain()
            self._call("platform_service", "getvolume")
            self._wait(match=lambda t, p: isinstance(p, dict) and p.get("volume_type") == 0)
        return {"volume_value": self.volume, "muted": self.muted}

    def get_state(self):
        # NOTE: on modern firmware `gettvstate` is the PAIRING PIN trigger, not a
        # state query — never poll it. State only arrives via broadcast pushes
        # (captured into self.state by _on_message), when the TV chooses to send.
        return self.state
