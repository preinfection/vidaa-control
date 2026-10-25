#!/usr/bin/env python3
"""Modern VIDAA pairing (transport_protocol >= 3290).

Newer firmware does NOT show the 4-digit PIN in response to the old
`gettvstate` trigger that pair_daemon.py uses. It needs the RemoteNOW app
handshake: publish `vidaa_app_connect`, the TV shows a PIN, submit it with
`authenticationcode` (authNum as an INT), then request an access token with
`gettoken`. The token is saved to keys/.token.json for reuse.

Run this with the TV ON (not just standby) and someone able to read the PIN.
Put the PIN in server/.paircode (or type it when prompted), e.g.:
    echo 1234 > server/.paircode
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tv

CODE_FILE = os.path.join(HERE, ".paircode")
TOKEN_FILE = os.path.join(HERE, "keys", ".token.json")


def log(m):
    print(time.strftime("%H:%M:%S"), m, flush=True)


def main():
    global TOKEN_FILE
    host = os.environ.get("HISENSE_HOST", "10.0.0.50")
    TOKEN_FILE = tv._token_path(host)

    if os.path.exists(CODE_FILE):
        os.remove(CODE_FILE)
    # re-pair: drop any old token so the pairing connect uses dynamic creds
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)

    conn = tv.HisenseTV(host=host, timeout=15.0)
    dev = conn._device

    captured = []
    _orig = conn._on_message

    def on_msg(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", "strict"))
        except Exception:
            payload = msg.payload
        captured.append((msg.topic, payload))
        log(f"  << {msg.topic}")
        log(f"     {payload}")
        return _orig(client, userdata, msg)

    conn._c.on_message = on_msg

    def on_connect(client, userdata, flags, rc, props=None):
        if getattr(rc, "is_failure", False):
            conn.connect_error = str(rc)
            return
        # subscribe to the mobile/auth topics the TV replies on
        for t in (
            f"/remoteapp/mobile/{dev}/#",
            "/remoteapp/mobile/broadcast/#",
            f"/remoteapp/mobile/{dev}/ui_service/data/authentication",
            f"/remoteapp/mobile/{dev}/ui_service/data/authenticationcode",
            f"/remoteapp/mobile/{dev}/ui_service/data/vidaa_app_connect",
            f"/remoteapp/mobile/{dev}/platform_service/data/tokenissuance",
            "/remoteapp/#",
        ):
            client.subscribe(t)
        conn.connected.set()

    conn._c.on_connect = on_connect

    log(f"connecting to {conn.host}:{conn.port} (mode={conn.mode}, cert={os.path.basename(tv.CERT)}) ...")
    conn.connect()
    log(f"CONNACK success. device id = {dev}")

    # 1) trigger the PIN dialog on the TV (retry up to 3x like the app does)
    for attempt in range(1, 4):
        log(f"publishing vidaa_app_connect (attempt {attempt}/3) -> TV should display a 4-digit PIN")
        conn._call("ui_service", "vidaa_app_connect",
                   {"app_version": 2, "connect_result": 0, "device_type": "Mobile App"})
        time.sleep(4)
        if any("authentication" in str(t) for t, _ in captured):
            log("TV sent an authentication push — PIN dialog should be up.")
            break

    # 2) wait for the PIN (file or stdin)
    log(f"waiting for PIN in {CODE_FILE} (up to 600s) ...")
    deadline = time.time() + 600
    code = None
    while time.time() < deadline:
        if os.path.exists(CODE_FILE):
            code = open(CODE_FILE).read().strip()
            if code:
                break
        time.sleep(1)
    if not code:
        log("no PIN provided; aborting. (TV on? PIN shown?)")
        log(f"messages seen during wait: {len(captured)}")
        conn.disconnect()
        return

    # 3) submit the PIN (authNum as INT for modern firmware)
    log(f"submitting PIN {code!r} via authenticationcode ...")
    conn._drain()
    try:
        conn._call("ui_service", "authenticationcode", {"authNum": int(code)})
    except ValueError:
        conn._call("ui_service", "authenticationcode", {"authNum": code})

    result = None
    t_end = time.time() + 15
    while time.time() < t_end:
        try:
            topic, payload = conn._q.get(timeout=1.0)
        except Exception:
            continue
        if isinstance(payload, dict) and "result" in payload:
            result = payload
            break
    log(f"authenticationcode result: {result}")

    if not (isinstance(result, dict) and int(result.get("result", 0)) == 1):
        log("PIN not accepted (or no reply). Stopping before token request.")
        conn.disconnect()
        return
    log("PIN ACCEPTED.")

    # 4) request an access token, close the dialog
    log("requesting token via platform_service/data/gettoken ...")
    conn._drain()
    # gettoken goes to the data/ path, NOT actions/ (that's what _call builds)
    gettoken_topic = f"/remoteapp/tv/platform_service/{conn._device}/data/gettoken"
    conn._c.publish(gettoken_topic, json.dumps({"refreshtoken": ""})).wait_for_publish()
    conn._call("ui_service", "authenticationcodeclose", None)
    token = None
    t_end = time.time() + 15
    while time.time() < t_end:
        try:
            topic, payload = conn._q.get(timeout=1.0)
        except Exception:
            continue
        if isinstance(payload, dict) and ("accesstoken" in payload or "refreshtoken" in payload):
            token = payload
            break
    if token:
        # persist the pairing client_id + username; the token connect needs them
        token["client_id"] = conn.client_id
        token["username"] = conn.username
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump(token, f)
        log(f"TOKEN SAVED to {TOKEN_FILE} (+client_id +username)")
    else:
        log("no token returned — cannot authorize commands; aborting.")
        conn.disconnect()
        return

    # 4b) probe app/source list on the STILL-LIVE pairing (dynamic-creds) session
    log("probing applist/sourcelist on the pairing session ...")
    conn._call("ui_service", "applist")
    time.sleep(3)
    conn._call("ui_service", "sourcelist")
    time.sleep(3)

    def _lists(key_any):
        out = []
        for _t, p in captured:
            if isinstance(p, list) and p and isinstance(p[0], dict) and any(k in p[0] for k in key_any):
                out.append(p)
        return out

    apps = _lists(("appId", "appName", "name"))
    srcs = _lists(("sourceid", "sourcename", "displayname"))
    if apps:
        with open(os.path.join(HERE, "keys", f".apps_{host}.json"), "w") as f:
            json.dump(apps[-1], f)
        log(f"*** APP LIST CAPTURED: {len(apps[-1])} apps -> keys/.apps_{host}.json ***")
    else:
        log("no applist response (even on the pairing session)")
    if srcs:
        with open(os.path.join(HERE, "keys", f".sources_{host}.json"), "w") as f:
            json.dump(srcs[-1], f)
        log(f"*** SOURCE LIST CAPTURED: {len(srcs[-1])} sources ***")
    else:
        log("no sourcelist response")

    conn.disconnect()

    # 5) verify with a fresh TOKEN-authenticated connection (how the bridge runs)
    time.sleep(1.5)
    log("reconnecting with the saved token to verify control ...")
    v = tv.HisenseTV(host=conn.host, timeout=12.0)
    log(f"  using_token = {v.using_token}")
    got = {}

    def vcap(client, userdata, msg):
        try:
            got[msg.topic.split("/")[-1]] = json.loads(msg.payload.decode("utf-8", "strict"))
        except Exception:
            got[msg.topic.split("/")[-1]] = msg.payload

    v._c.on_message = vcap

    def vconn(client, userdata, flags, rc, props=None):
        client.subscribe("/remoteapp/#")
        v.connected.set()

    v._c.on_connect = vconn
    v.connect()
    for svc, act in [("ui_service", "gettvstate"), ("platform_service", "getvolume")]:
        v._call(svc, act)
        time.sleep(2.5)
    if got:
        log(f"CONTROL OK — TV responded on: {list(got.keys())}")
        log("*** PAIRING COMPLETE — the launcher can now control the TV. ***")
    else:
        log("token reconnect still silent — needs more investigation.")
    v.disconnect()


if __name__ == "__main__":
    main()
