#!/usr/bin/env python3
"""PaperStage web app (EC2 side). Stdlib only.

Serves the static storefront under /clip/, accepts paper-cutout film
orders (multipart form + one or two photos), verifies Gumroad license
keys (fail-closed) to mark orders paid, and exposes a token-protected
production API: the producer lists paid orders, downloads the photos,
and uploads the finished film (MP4) for delivery.

Data layout (next to this file):
  data/orders/<order_id>/order.json, photo1.jpg|photo1.png,
                          photo2.jpg|photo2.png, delivery.mp4
  data/licenses.json  {license_key: {"order_id": ..., "used_at": ...}}
  clip_config.json    {gumroad_product_id, gumroad_buy_url, agent_token}
"""
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(BASE_DIR, "site")
MEDIA_DIR = os.path.join(SITE_DIR, "media")
DATA_DIR = os.path.join(BASE_DIR, "data")
ORDERS_DIR = os.path.join(DATA_DIR, "orders")
CONFIG_PATH = os.path.join(BASE_DIR, "clip_config.json")
LICENSES_PATH = os.path.join(DATA_DIR, "licenses.json")
MAX_FILMS_PER_KEY = 1
DAILY_ORDER_CAP_PER_IP = 20
UNPAID_TTL_SECONDS = 7 * 24 * 3600
ORDER_TTL_SECONDS = 60 * 24 * 3600
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_FILM_BYTES = 200 * 1024 * 1024
VARIANTS = {"solo", "duo"}
SCENES = {
    "christmas-market": "Christmas Market",
    "london-snow": "London Snow",
    "paris-noel": "Paris Noël",
    "new-york-christmas": "New York Christmas",
    "vienna-christmas": "Vienna Christmas",
    "tokyo-christmas": "Tokyo Christmas",
}
STATUS_LABELS = {
    "received": "Order received",
    "paid": "Paid — in production",
    "delivered": "Delivered",
}
MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
}

_lock = threading.Lock()


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def order_dir(order_id):
    return os.path.join(ORDERS_DIR, order_id)


def read_order(order_id):
    if not order_id or "/" in order_id or "\\" in order_id or ".." in order_id:
        return None
    p = os.path.join(order_dir(order_id), "order.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def write_order(order):
    d = order_dir(order["id"])
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "order.json.tmp")
    with open(tmp, "w") as f:
        json.dump(order, f)
    os.replace(tmp, os.path.join(d, "order.json"))


def load_licenses():
    if not os.path.exists(LICENSES_PATH):
        return {}
    with open(LICENSES_PATH) as f:
        return json.load(f)


def save_licenses(data):
    tmp = LICENSES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, LICENSES_PATH)


def cleanup_old_orders():
    now = time.time()
    try:
        for name in os.listdir(ORDERS_DIR):
            d = order_dir(name)
            if not os.path.isdir(d):
                continue
            order = read_order(name)
            age = now - os.path.getmtime(d)
            if order and order.get("status") == "received" and age > UNPAID_TTL_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
            elif age > ORDER_TTL_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
    except FileNotFoundError:
        pass


def verify_gumroad_license(product_id, license_key):
    """Returns (ok, info). Uses Gumroad's public license verification API."""
    body = urllib.parse.urlencode({
        "product_id": product_id,
        "license_key": license_key,
        "increment_uses_count": "false",
    }).encode()
    req = urllib.request.Request(
        "https://api.gumroad.com/v2/licenses/verify", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode())
        except Exception:
            return False, {"error": "verify_http_%s" % e.code}
    except Exception as e:
        return False, {"error": "verify_failed: %s" % e}
    if not data.get("success"):
        return False, data
    purchase = data.get("purchase") or {}
    if purchase.get("refunded") or purchase.get("disputed") or purchase.get("chargebacked"):
        return False, {"error": "purchase_not_valid"}
    return True, data


def clean_text(value, limit):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def photo_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type, download_name=None, cache=True):
        if not os.path.exists(path):
            self.send_json(404, {"error": "not_found"})
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=86400")
        if download_name:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % download_name)
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode())

    def parse_multipart(self):
        """Minimal multipart/form-data parser (stdlib only).

        Returns (fields, files): fields {name: str},
        files {name: (filename, bytes)}.
        """
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return {}, {}
        boundary = ("--" + m.group(1).strip().strip('"')).encode()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        fields, files = {}, {}
        for part in body.split(boundary):
            if part.startswith(b"\r\n"):
                part = part[2:]
            if part.endswith(b"--"):
                part = part[:-2]
            if part.endswith(b"\r\n"):
                part = part[:-2]
            if not part:
                continue
            head, sep, content = part.partition(b"\r\n\r\n")
            if not sep:
                continue
            headers = head.decode("utf-8", "replace")
            name_m = re.search(r'name="([^"]+)"', headers)
            if not name_m:
                continue
            name = name_m.group(1)
            fn_m = re.search(r'filename="([^"]*)"', headers)
            if fn_m and fn_m.group(1):
                files[name] = (fn_m.group(1), content)
            else:
                fields[name] = content.decode("utf-8", "replace")
        return fields, files

    def agent_authorized(self):
        try:
            token = load_config().get("agent_token", "")
        except Exception:
            return False
        return bool(token) and self.headers.get("X-Agent-Token") == token

    # ---------------- GET ----------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/clip", "/clip/", "/clip/index.html"):
            self.send_file(os.path.join(SITE_DIR, "index.html"),
                           "text/html; charset=utf-8", cache=False)
            return
        if path == "/clip/llms.txt":
            self.send_file(os.path.join(SITE_DIR, "llms.txt"),
                           "text/plain; charset=utf-8", cache=False)
            return
        if path.startswith("/clip/media/"):
            rel = os.path.normpath(path[len("/clip/media/"):])
            full = os.path.join(MEDIA_DIR, rel)
            if rel.startswith("..") or not full.startswith(MEDIA_DIR):
                self.send_json(404, {"error": "not_found"})
                return
            ctype = MEDIA_TYPES.get(os.path.splitext(rel)[1].lower())
            if not ctype:
                self.send_json(404, {"error": "not_found"})
                return
            self.send_file(full, ctype)
            return
        if path == "/clip/health":
            self.send_json(200, {"ok": True})
            return
        if path == "/clip/api/config":
            try:
                cfg = load_config()
                self.send_json(200, {"buy_url": cfg.get("gumroad_buy_url", "")})
            except Exception:
                self.send_json(200, {"buy_url": ""})
            return
        if path.startswith("/clip/files/"):
            parts = path.split("/")
            # /clip/files/<order_id>/<token>/film.mp4
            if len(parts) == 6:
                order_id, token, name = parts[3], parts[4], parts[5]
                order = read_order(order_id)
                if order and order.get("status") == "delivered" and token \
                        and token == order.get("delivery_token"):
                    if name == "film.mp4":
                        self.send_file(os.path.join(order_dir(order_id), "delivery.mp4"),
                                       "video/mp4",
                                       download_name="paperstage-christmas-film.mp4")
                        return
                self.send_json(403, {"error": "locked"})
                return
            self.send_json(404, {"error": "not_found"})
            return
        if path.startswith("/clip/api/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            order = read_order(order_id)
            if not order:
                self.send_json(404, {"error": "not_found"})
                return
            out = {
                "id": order["id"],
                "status": order["status"],
                "status_label": STATUS_LABELS.get(order["status"], order["status"]),
                "variant": order["variant"],
                "variant_label": "Duo — two people" if order["variant"] == "duo" else "Solo — one person",
                "scene": order["scene"],
                "scene_label": SCENES.get(order["scene"], order["scene"]),
                "name1": order.get("name1", ""),
                "name2": order.get("name2", ""),
                "created_at": order["created_at"],
                "paid_at": order.get("paid_at"),
                "delivered_at": order.get("delivered_at"),
            }
            if order["status"] == "delivered":
                out["film_url"] = "/clip/files/%s/%s/film.mp4" % (
                    order_id, order["delivery_token"])
            self.send_json(200, out)
            return
        if path == "/clip/agent/paid":
            if not self.agent_authorized():
                self.send_json(403, {"error": "forbidden"})
                return
            paid = []
            try:
                ids = sorted(os.listdir(ORDERS_DIR))
            except FileNotFoundError:
                ids = []
            for oid in ids:
                order = read_order(oid)
                if order and order.get("status") == "paid":
                    photo_files = order["photo_files"]
                    paid.append({
                        "id": order["id"],
                        "variant": order["variant"],
                        "scene": order["scene"],
                        "scene_label": SCENES.get(order["scene"], order["scene"]),
                        "name1": order.get("name1", ""),
                        "name2": order.get("name2", ""),
                        "email": order["email"],
                        "note": order.get("note", ""),
                        "license_key_last4": order.get("license_key_last4", ""),
                        "paid_at": order.get("paid_at"),
                        "photo_files": photo_files,
                        "photo_paths": [os.path.join("data", "orders", oid, pf)
                                        for pf in photo_files],
                        "photo_urls": ["/clip/agent/orders/%s/photo/%d" % (oid, i + 1)
                                       for i in range(len(photo_files))],
                    })
            paid.sort(key=lambda o: o.get("paid_at") or 0)
            self.send_json(200, {"orders": paid})
            return
        m = re.match(r"^/clip/agent/orders/([^/]+)/photo/(\d+)$", path)
        if m:
            if not self.agent_authorized():
                self.send_json(403, {"error": "forbidden"})
                return
            order_id, n = m.group(1), int(m.group(2))
            order = read_order(order_id)
            if not order or not (1 <= n <= len(order["photo_files"])):
                self.send_json(404, {"error": "not_found"})
                return
            fname = order["photo_files"][n - 1]
            photo = os.path.join(order_dir(order_id), fname)
            ctype = "image/png" if fname.endswith(".png") else "image/jpeg"
            self.send_file(photo, ctype, cache=False)
            return
        self.send_json(404, {"error": "not_found"})

    # ---------------- POST ----------------

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/clip/api/orders":
            fields, files = self.parse_multipart()
            variant = clean_text(fields.get("variant", ""), 10)
            scene = clean_text(fields.get("scene", ""), 40)
            name1 = clean_text(fields.get("name1", ""), 40)
            name2 = clean_text(fields.get("name2", ""), 40)
            email = clean_text(fields.get("email", ""), 120)
            note = clean_text(fields.get("note", ""), 500)
            if variant not in VARIANTS:
                self.send_json(400, {"error": "bad_variant"})
                return
            if scene not in SCENES:
                self.send_json(400, {"error": "bad_scene"})
                return
            if "@" not in email or "." not in email:
                self.send_json(400, {"error": "bad_email"})
                return
            want = 2 if variant == "duo" else 1
            photos = []
            for i in range(1, want + 1):
                photo = files.get("photo%d" % i)
                if not photo:
                    self.send_json(400, {"error": "missing_photo%d" % i})
                    return
                _, data = photo
                if not (0 < len(data) <= MAX_PHOTO_BYTES):
                    self.send_json(400, {"error": "photo%d_too_large" % i})
                    return
                ext = photo_ext(data)
                if not ext:
                    self.send_json(400, {"error": "photo%d_must_be_jpg_or_png" % i})
                    return
                photos.append(("photo%d%s" % (i, ext), data))
            ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
            today = time.strftime("%Y-%m-%d")
            with _lock:
                counts_path = os.path.join(DATA_DIR, "ip_counts.json")
                counts = {}
                if os.path.exists(counts_path):
                    with open(counts_path) as f:
                        counts = json.load(f)
                key = "%s:%s" % (today, ip)
                if counts.get(key, 0) >= DAILY_ORDER_CAP_PER_IP:
                    self.send_json(429, {"error": "daily_cap"})
                    return
                counts[key] = counts.get(key, 0) + 1
                with open(counts_path, "w") as f:
                    json.dump(counts, f)
            cleanup_old_orders()
            order_id = secrets.token_urlsafe(12)
            d = order_dir(order_id)
            os.makedirs(d, exist_ok=True)
            for fname, data in photos:
                with open(os.path.join(d, fname), "wb") as f:
                    f.write(data)
            order = {
                "id": order_id,
                "status": "received",
                "variant": variant,
                "scene": scene,
                "name1": name1,
                "name2": name2,
                "email": email,
                "note": note,
                "photo_files": [fname for fname, _ in photos],
                "license_verified": False,
                "license_key_last4": "",
                "created_at": time.time(),
                "paid_at": None,
                "delivered_at": None,
                "delivery_token": secrets.token_urlsafe(18),
            }
            write_order(order)
            self.send_json(200, {"id": order_id, "status": "received"})
            return

        if path.startswith("/clip/api/orders/") and path.endswith("/activate"):
            order_id = path.split("/")[4]
            order = read_order(order_id)
            if not order:
                self.send_json(404, {"error": "not_found"})
                return
            try:
                payload = self.read_json_body()
            except Exception:
                self.send_json(400, {"error": "bad_json"})
                return
            license_key = (payload.get("license_key") or "").strip().upper()
            if not license_key:
                self.send_json(400, {"error": "missing_license_key"})
                return
            if order["status"] in ("paid", "delivered") and order.get("license_verified"):
                self.send_json(200, {"activated": True, "status": order["status"]})
                return
            try:
                cfg = load_config()
            except Exception:
                self.send_json(500, {"error": "config_missing"})
                return
            with _lock:
                licenses = load_licenses()
                rec = licenses.get(license_key)
                if rec and rec.get("order_id") != order_id:
                    self.send_json(403, {"error": "license_already_used"})
                    return
                ok, info = verify_gumroad_license(
                    cfg.get("gumroad_product_id", ""), license_key)
                if not ok:
                    self.send_json(403, {"error": "license_invalid",
                                         "detail": info.get("message", "")})
                    return
                licenses[license_key] = {"order_id": order_id,
                                         "used_at": time.time()}
                save_licenses(licenses)
                order["status"] = "paid"
                order["license_verified"] = True
                order["license_key_last4"] = license_key[-4:]
                order["paid_at"] = time.time()
                write_order(order)
            self.send_json(200, {"activated": True, "status": "paid"})
            return

        if path.startswith("/clip/agent/deliver/"):
            if not self.agent_authorized():
                self.send_json(403, {"error": "forbidden"})
                return
            order_id = path.rsplit("/", 1)[-1]
            order = read_order(order_id)
            if not order:
                self.send_json(404, {"error": "not_found"})
                return
            fields, files = self.parse_multipart()
            film = files.get("mp4")
            if not film:
                self.send_json(400, {"error": "missing_mp4"})
                return
            _, film_data = film
            if len(film_data) > MAX_FILM_BYTES or len(film_data) < 12 \
                    or film_data[4:8] != b"ftyp":
                self.send_json(400, {"error": "bad_mp4"})
                return
            d = order_dir(order_id)
            with open(os.path.join(d, "delivery.mp4"), "wb") as f:
                f.write(film_data)
            order["status"] = "delivered"
            order["delivered_at"] = time.time()
            write_order(order)
            self.send_json(200, {"ok": True, "status": "delivered",
                                 "film_url": "/clip/files/%s/%s/film.mp4" % (
                                     order_id, order["delivery_token"])})
            return

        self.send_json(404, {"error": "not_found"})


def main():
    os.makedirs(ORDERS_DIR, exist_ok=True)
    port = int(os.environ.get("CLIP_PORT", "8794"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("clip app listening on 127.0.0.1:%d" % port)
    server.serve_forever()


if __name__ == "__main__":
    main()
