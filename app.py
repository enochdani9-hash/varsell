import os
import re
import gzip
import json
import time
import sqlite3
import secrets
import string
import base64
import threading
import urllib.request
from datetime import timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, jsonify
from markupsafe import Markup, escape

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "varsell-super-secret-dev-key-2026")
app.permanent_session_lifetime = timedelta(days=30)
ADMIN_MASTER_KEY = os.environ.get("ADMIN_KEY", "VARSELL-BOSS-2026")

DB_NAME = "varsell.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_POSTGRES = bool(DATABASE_URL and psycopg2)

RESERVED_SLUGS = {
    "login", "logout", "dashboard", "admin", "admin-logout", "create-store",
    "change-key", "change-store-name", "update-store", "update-theme",
    "update-avatar", "add-product", "edit-product", "manifest.json", "sw.js",
    "icon.svg", "sitemap.xml", "robots.txt", "store", "ping"
}


# --- SPEED BOOST 1: AUTOMATIC GZIP COMPRESSION FOR FAST MOBILE LOADING ---
@app.after_request
def compress_response(response):
    accept_encoding = request.headers.get("Accept-Encoding", "")
    if (
        "gzip" not in accept_encoding.lower()
        or response.status_code < 200
        or response.status_code >= 300
        or response.direct_passthrough
        or "Content-Encoding" in response.headers
        or not (
            response.content_type.startswith("text/")
            or response.content_type.startswith("application/json")
            or response.content_type.startswith("application/javascript")
        )
    ):
        return response

    data = response.get_data()
    if len(data) < 500:
        return response

    gzip_buffer = gzip.compress(data, compresslevel=5)
    response.set_data(gzip_buffer)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = len(gzip_buffer)
    response.headers["Vary"] = "Accept-Encoding"
    return response


# --- SPEED BOOST 2: SELF-PING BACKGROUND THREAD TO PREVENT RENDER SLEEP ---
def keep_render_awake():
    """Pings the live Render URL every 8 minutes from inside the server itself"""
    while True:
        time.sleep(480)
        ext_url = os.environ.get("RENDER_EXTERNAL_URL", "https://varsell.onrender.com").rstrip("/")
        try:
            urllib.request.urlopen(f"{ext_url}/ping", timeout=10)
        except Exception:
            pass


threading.Thread(target=keep_render_awake, daemon=True).start()


@app.template_filter("format_desc")
def format_desc_filter(text):
    if not text:
        return ""
    safe_text = str(escape(text))
    formatted = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe_text)
    formatted = formatted.replace("\n", "<br>")
    return Markup(formatted)


# --- SPEED BOOST 3: PERSISTENT REUSABLE DATABASE CONNECTION ---
_pg_conn = None
_pg_lock = threading.Lock()


def get_raw_pg_conn():
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed != 0:
        _pg_conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        _pg_conn.autocommit = False
    return _pg_conn


class DBWrapper:
    def __init__(self):
        if IS_POSTGRES:
            self.conn = get_raw_pg_conn()
        else:
            self.conn = sqlite3.connect(DB_NAME, timeout=15)
            self.conn.row_factory = sqlite3.Row

    def execute(self, query, params=()):
        if IS_POSTGRES:
            pg_query = query.replace("?", "%s")
            try:
                cursor = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cursor.execute(pg_query, params)
                return cursor
            except Exception:
                # Reconnect automatically if idle connection was reset by cloud provider
                global _pg_conn
                try:
                    if _pg_conn and _pg_conn.closed == 0:
                        _pg_conn.rollback()
                except Exception:
                    pass
                _pg_conn = None
                self.conn = get_raw_pg_conn()
                cursor = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cursor.execute(pg_query, params)
                return cursor
        else:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            return cursor

    def commit(self):
        try:
            self.conn.commit()
        except Exception:
            pass

    def close(self):
        # Keep PostgreSQL connection warm in memory for instant subsequent requests
        if not IS_POSTGRES:
            self.conn.close()


def get_db_connection():
    return DBWrapper()


def init_db():
    conn = get_db_connection()
    if IS_POSTGRES:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                id SERIAL PRIMARY KEY,
                store_name TEXT NOT NULL,
                store_slug TEXT UNIQUE NOT NULL,
                access_key TEXT UNIQUE NOT NULL,
                whatsapp_number TEXT NOT NULL,
                category TEXT DEFAULT 'General',
                bio TEXT DEFAULT '',
                logo_data TEXT DEFAULT '',
                banner_data TEXT DEFAULT '',
                bg_image_data TEXT DEFAULT '',
                theme_color TEXT DEFAULT 'emerald',
                country TEXT DEFAULT 'Global',
                default_currency TEXT DEFAULT 'USD',
                payment_method_label TEXT DEFAULT 'Mobile Money / Instant Transfer',
                momo_number TEXT DEFAULT '',
                momo_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        new_store_cols = [
            ("logo_data", "TEXT DEFAULT ''"),
            ("banner_data", "TEXT DEFAULT ''"),
            ("bg_image_data", "TEXT DEFAULT ''"),
            ("theme_color", "TEXT DEFAULT 'emerald'"),
            ("country", "TEXT DEFAULT 'Global'"),
            ("default_currency", "TEXT DEFAULT 'USD'"),
            ("payment_method_label", "TEXT DEFAULT 'Mobile Money / Instant Transfer'"),
            ("momo_number", "TEXT DEFAULT ''"),
            ("momo_name", "TEXT DEFAULT ''")
        ]
        for col_name, col_def in new_store_cols:
            conn.execute(f"ALTER TABLE stores ADD COLUMN IF NOT EXISTS {col_name} {col_def}")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_aliases (
                id SERIAL PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                old_slug TEXT UNIQUE NOT NULL,
                old_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                product_name TEXT NOT NULL,
                price REAL NOT NULL,
                compare_price REAL DEFAULT 0,
                badge_text TEXT DEFAULT '',
                variants_text TEXT DEFAULT '',
                currency TEXT DEFAULT 'USD',
                description TEXT DEFAULT '',
                image_data TEXT NOT NULL,
                images_json TEXT DEFAULT '[]',
                in_stock INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS images_json TEXT DEFAULT '[]'")
        conn.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS compare_price REAL DEFAULT 0")
        conn.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS badge_text TEXT DEFAULT ''")
        conn.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS variants_text TEXT DEFAULT ''")
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_name TEXT NOT NULL,
                store_slug TEXT UNIQUE NOT NULL,
                access_key TEXT UNIQUE NOT NULL,
                whatsapp_number TEXT NOT NULL,
                category TEXT DEFAULT 'General',
                bio TEXT DEFAULT '',
                logo_data TEXT DEFAULT '',
                banner_data TEXT DEFAULT '',
                bg_image_data TEXT DEFAULT '',
                theme_color TEXT DEFAULT 'emerald',
                country TEXT DEFAULT 'Global',
                default_currency TEXT DEFAULT 'USD',
                payment_method_label TEXT DEFAULT 'Mobile Money / Instant Transfer',
                momo_number TEXT DEFAULT '',
                momo_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col_name, col_def in [
            ("logo_data", "TEXT DEFAULT ''"),
            ("banner_data", "TEXT DEFAULT ''"),
            ("bg_image_data", "TEXT DEFAULT ''"),
            ("theme_color", "TEXT DEFAULT 'emerald'"),
            ("country", "TEXT DEFAULT 'Global'"),
            ("default_currency", "TEXT DEFAULT 'USD'"),
            ("payment_method_label", "TEXT DEFAULT 'Mobile Money / Instant Transfer'"),
            ("momo_number", "TEXT DEFAULT ''"),
            ("momo_name", "TEXT DEFAULT ''")
        ]:
            try:
                conn.execute(f"ALTER TABLE stores ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                old_slug TEXT UNIQUE NOT NULL,
                old_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (store_id) REFERENCES stores (id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                price REAL NOT NULL,
                compare_price REAL DEFAULT 0,
                badge_text TEXT DEFAULT '',
                variants_text TEXT DEFAULT '',
                currency TEXT DEFAULT 'USD',
                description TEXT DEFAULT '',
                image_data TEXT NOT NULL,
                images_json TEXT DEFAULT '[]',
                in_stock INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (store_id) REFERENCES stores (id) ON DELETE CASCADE
            )
        """)
        for col_name, col_def in [
            ("images_json", "TEXT DEFAULT '[]'"),
            ("compare_price", "REAL DEFAULT 0"),
            ("badge_text", "TEXT DEFAULT ''"),
            ("variants_text", "TEXT DEFAULT ''")
        ]:
            try:
                conn.execute(f"ALTER TABLE products ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass

    conn.commit()
    conn.close()


init_db()


def generate_access_key():
    digits = "".join(secrets.choice(string.digits) for _ in range(4))
    return f"VS-{digits}"


def normalize_key(raw_key):
    cleaned = re.sub(r"\s+", "-", raw_key.strip().upper())
    cleaned = re.sub(r"[^A-Z0-9_-]", "", cleaned)
    return cleaned


def create_slug(store_name):
    clean_name = re.sub(r"\.com|\.gh|\.net|\.org|\.store", "", store_name.lower())
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", clean_name).strip().lower()
    slug = re.sub(r"[\s-]+", "-", slug)
    if not slug or slug in RESERVED_SLUGS:
        slug = f"{slug or 'shop'}-store"
    return slug


def clean_international_whatsapp(number, country_code=""):
    cleaned = re.sub(r"[^\d]", "", number)
    dial = re.sub(r"[^\d]", "", country_code)

    if not cleaned:
        return ""

    if dial:
        if cleaned.startswith("0"):
            cleaned = dial + cleaned[1:]
        elif not cleaned.startswith(dial) and len(cleaned) <= 10:
            cleaned = dial + cleaned
    else:
        if len(cleaned) == 10 and cleaned.startswith("0"):
            cleaned = "233" + cleaned[1:]
    return cleaned


def encode_uploaded_image(file_obj):
    if file_obj and getattr(file_obj, "filename", "") != "":
        file_bytes = file_obj.read()
        if file_bytes:
            mime_type = file_obj.mimetype or "image/jpeg"
            b64_encoded = base64.b64encode(file_bytes).decode("utf-8")
            return f"data:{mime_type};base64,{b64_encoded}"
    return ""


def hydrate_products(product_rows):
    """Hydrates multi-image list, calculates discount %, and parses variants (Sizes/Colors)"""
    hydrated = []
    for row in product_rows:
        p = dict(row)
        imgs = []
        raw_json = p.get("images_json")
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, list):
                    imgs = [str(x) for x in parsed if x]
            except Exception:
                pass
        if not imgs and p.get("image_data"):
            imgs = [p["image_data"]]
        if not imgs:
            imgs = ["https://placehold.co/600x600/0f172a/10b981?text=Varsell+Product"]
        p["images_list"] = imgs
        p["image_data"] = imgs[0]

        # Parse comma-separated variants (e.g. "Size 32, Size 34, Black")
        raw_variants = (p.get("variants_text") or "").strip()
        p["variants_text"] = raw_variants
        if raw_variants:
            p["variants_list"] = [v.strip() for v in raw_variants.split(",") if v.strip()]
        else:
            p["variants_list"] = []

        # Calculate discount percentage and savings amount
        price_val = float(p.get("price") or 0.0)
        compare_val = float(p.get("compare_price") or 0.0)
        p["compare_price"] = compare_val
        p["badge_text"] = (p.get("badge_text") or "").strip()

        if compare_val > price_val and price_val > 0:
            pct = int(round(((compare_val - price_val) / compare_val) * 100))
            p["discount_percent"] = max(1, min(99, pct))
            p["savings_amount"] = compare_val - price_val
        else:
            p["discount_percent"] = 0
            p["savings_amount"] = 0.0

        hydrated.append(p)
    return hydrated


def save_alias_if_needed(conn, store_id, old_slug, old_name):
    existing = conn.execute(
        "SELECT id FROM store_aliases WHERE old_slug = ?", (old_slug,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO store_aliases (store_id, old_slug, old_name) VALUES (?, ?, ?)",
            (store_id, old_slug, old_name)
        )


def find_store_by_any_identifier(conn, identifier):
    raw = identifier.strip()
    slug_candidate = create_slug(raw)
    compact_candidate = re.sub(r"[^a-z0-9]", "", raw.lower())

    store = conn.execute(
        "SELECT * FROM stores WHERE store_slug = ? OR store_slug = ?",
        (raw.lower(), slug_candidate)
    ).fetchone()
    if store:
        return dict(store), False

    store = conn.execute(
        "SELECT * FROM stores WHERE LOWER(store_name) = ?",
        (raw.lower(),)
    ).fetchone()
    if store:
        return dict(store), True

    alias = conn.execute(
        "SELECT store_id FROM store_aliases WHERE old_slug = ? OR old_slug = ? OR LOWER(old_name) = ?",
        (raw.lower(), slug_candidate, raw.lower())
    ).fetchone()
    if alias:
        store = conn.execute("SELECT * FROM stores WHERE id = ?", (alias["store_id"],)).fetchone()
        if store:
            return dict(store), True

    if compact_candidate:
        all_stores = conn.execute("SELECT id, store_name, store_slug FROM stores").fetchall()
        for s in all_stores:
            s_dict = dict(s)
            s_compact = re.sub(r"[^a-z0-9]", "", s_dict["store_slug"].lower())
            name_compact = re.sub(r"[^a-z0-9]", "", s_dict["store_name"].lower())
            if compact_candidate in (s_compact, name_compact):
                full_store = conn.execute("SELECT * FROM stores WHERE id = ?", (s_dict["id"],)).fetchone()
                return dict(full_store), True

    return None, False


# --- ULTRA-LIGHTWEIGHT KEEP-ALIVE PING ROUTE ---

@app.route("/ping")
def ping():
    return Response("OK", status=200, mimetype="text/plain")


# --- PWA, OFFLINE & SEO ROUTES ---

@app.route("/icon.svg")
def app_icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
      <defs>
        <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#34d399"/>
          <stop offset="100%" stop-color="#0d9488"/>
        </linearGradient>
      </defs>
      <rect width="512" height="512" rx="115" fill="#04070f"/>
      <rect x="36" y="36" width="440" height="440" rx="95" fill="url(#g)"/>
      <text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" font-family="sans-serif" font-weight="900" font-size="290" fill="#04070f">V</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Varsell — Global Social & WhatsApp Store Platform",
        "short_name": "Varsell",
        "description": "Launch and customize your online store in 60 seconds. Works offline worldwide.",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#04070f",
        "theme_color": "#10b981",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "192x192 512x512",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }
        ]
    })


@app.route("/sw.js")
def service_worker():
    sw_code = """
const CACHE_NAME = 'varsell-global-v9';
const CORE_ASSETS = [
  '/',
  '/manifest.json',
  '/icon.svg',
  'https://cdn.tailwindcss.com'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET' || event.request.url.includes('/ping')) return;

  event.respondWith(
    fetch(event.request)
      .then((networkResponse) => {
        const clone = networkResponse.clone();
        caches.open(CACHE_NAME).then((cache) => {
          cache.put(event.request, clone);
        });
        return networkResponse;
      })
      .catch(() => {
        return caches.match(event.request).then((cachedResponse) => {
          if (cachedResponse) return cachedResponse;
          return caches.match('/');
        });
      })
  );
});
"""
    return Response(sw_code, mimetype="application/javascript")


@app.route("/robots.txt")
def robots_txt():
    host = request.host_url.rstrip("/")
    lines = ["User-agent: *", "Allow: /", f"Sitemap: {host}/sitemap.xml"]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    host = request.host_url.rstrip("/")
    conn = get_db_connection()
    stores = conn.execute("SELECT store_slug FROM stores ORDER BY id DESC").fetchall()
    conn.close()

    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    xml.append(f"  <url><loc>{host}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>")
    for s in stores:
        xml.append(f"  <url><loc>{host}/{s['store_slug']}</loc><changefreq>daily</changefreq><priority>0.9</priority></url>")
    xml.append("</urlset>")
    return Response("\n".join(xml), mimetype="application/xml")


# --- MAIN PLATFORM ROUTES ---

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    conn = get_db_connection()

    active_store = None
    active_key = session.get("store_key")
    if active_key:
        row = conn.execute(
            "SELECT store_name, store_slug FROM stores WHERE access_key = ?", (active_key,)
        ).fetchone()
        if row:
            active_store = dict(row)

    if q:
        like_q = f"%{q.lower()}%"
        recent_stores = conn.execute(
            """SELECT store_name, store_slug, category, bio, logo_data, momo_name, country, theme_color
               FROM stores
               WHERE LOWER(store_name) LIKE ? OR LOWER(store_slug) LIKE ? OR LOWER(category) LIKE ? OR LOWER(country) LIKE ?
               ORDER BY id DESC LIMIT 24""",
            (like_q, like_q, like_q, like_q)
        ).fetchall()
    else:
        recent_stores = conn.execute(
            "SELECT store_name, store_slug, category, bio, logo_data, momo_name, country, theme_color FROM stores ORDER BY id DESC LIMIT 12"
        ).fetchall()
    conn.close()
    return render_template("index.html", recent_stores=recent_stores, search_query=q, active_store=active_store)


@app.route("/create-store", methods=["POST"])
def create_store():
    store_name = request.form.get("store_name", "").strip()
    country_code = request.form.get("country_code", "233").strip()
    whatsapp_number = request.form.get("whatsapp_number", "").strip()
    custom_key_input = request.form.get("custom_key", "").strip()
    category = request.form.get("category", "General").strip()
    bio = request.form.get("bio", "").strip()
    country = request.form.get("country", "Global").strip()
    default_currency = request.form.get("default_currency", "USD").strip()
    payment_method_label = request.form.get("payment_method_label", "Mobile Money / Instant Transfer").strip()
    momo_number = request.form.get("momo_number", "").strip()
    momo_name = request.form.get("momo_name", "").strip()
    theme_color = request.form.get("theme_color", "emerald").strip()
    compressed_logo = request.form.get("compressed_logo", "").strip()
    logo_file = request.files.get("logo_file")

    if not store_name or not whatsapp_number:
        flash("Please provide both your Store Name and WhatsApp number.", "error")
        return redirect(url_for("index"))

    conn = get_db_connection()

    if custom_key_input:
        access_key = normalize_key(custom_key_input)
        if len(access_key) < 4:
            conn.close()
            flash("Your custom Secret Key must be at least 4 characters long.", "error")
            return redirect(url_for("index"))

        if access_key == normalize_key(ADMIN_MASTER_KEY):
            conn.close()
            flash("That key is reserved. Please pick a different Secret Key.", "error")
            return redirect(url_for("index"))

        existing_key = conn.execute(
            "SELECT id FROM stores WHERE access_key = ?", (access_key,)
        ).fetchone()
        if existing_key:
            conn.close()
            flash(f"The Secret Key '{access_key}' is already taken. If this is your store, enter '{access_key}' in the Unlock Store box!", "error")
            return redirect(url_for("index"))
    else:
        access_key = generate_access_key()
        while conn.execute("SELECT id FROM stores WHERE access_key = ?", (access_key,)).fetchone():
            access_key = generate_access_key()

    base_slug = create_slug(store_name)
    slug = base_slug
    counter = 1
    while conn.execute("SELECT id FROM stores WHERE store_slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{counter}"
        counter += 1

    formatted_wa = clean_international_whatsapp(whatsapp_number, country_code)
    logo_data = compressed_logo if compressed_logo.startswith("data:image") else encode_uploaded_image(logo_file)

    conn.execute(
        """INSERT INTO stores (
               store_name, store_slug, access_key, whatsapp_number, category, bio,
               logo_data, theme_color, country, default_currency, payment_method_label, momo_number, momo_name
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            store_name, slug, access_key, formatted_wa, category, bio,
            logo_data, theme_color, country, default_currency, payment_method_label, momo_number, momo_name
        )
    )
    conn.commit()
    conn.close()

    session.permanent = True
    session["store_key"] = access_key
    session["new_store_created"] = True
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["POST"])
def login():
    raw_input = request.form.get("access_key", "").strip()
    access_key = normalize_key(raw_input)

    if raw_input.upper() == ADMIN_MASTER_KEY.upper() or access_key == normalize_key(ADMIN_MASTER_KEY):
        session.permanent = True
        session["is_varsell_admin"] = True
        flash("⚡ Founder Master Key recognized! Welcome to Varsell Command HQ.", "success")
        return redirect(url_for("admin_panel"))

    conn = get_db_connection()
    store = conn.execute("SELECT id FROM stores WHERE access_key = ?", (access_key,)).fetchone()
    conn.close()

    if store:
        session.permanent = True
        session["store_key"] = access_key
        return redirect(url_for("dashboard"))
    else:
        flash("Secret Key not found. Double-check your key and try again.", "error")
        return redirect(url_for("index"))


@app.route("/change-key", methods=["POST"])
def change_key():
    current_key = session.get("store_key")
    if not current_key:
        return redirect(url_for("index"))

    new_key = normalize_key(request.form.get("new_key", ""))
    if len(new_key) < 4:
        flash("Your new Secret Key must be at least 4 characters long.", "error")
        return redirect(url_for("dashboard"))

    if new_key == normalize_key(ADMIN_MASTER_KEY):
        flash("That key is reserved. Please pick a different Secret Key.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    store = conn.execute("SELECT id FROM stores WHERE access_key = ?", (current_key,)).fetchone()
    if not store:
        conn.close()
        return redirect(url_for("index"))

    taken = conn.execute(
        "SELECT id FROM stores WHERE access_key = ? AND id != ?", (new_key, store["id"])
    ).fetchone()

    if taken:
        conn.close()
        flash(f"The key '{new_key}' is already in use by another store. Pick a different one!", "error")
        return redirect(url_for("dashboard"))

    conn.execute("UPDATE stores SET access_key = ? WHERE id = ?", (new_key, store["id"]))
    conn.commit()
    conn.close()

    session.permanent = True
    session["store_key"] = new_key
    flash(f"Your Store Secret Key has been updated to: {new_key}", "success")
    return redirect(url_for("dashboard"))


@app.route("/change-store-name", methods=["POST"])
def change_store_name():
    current_key = session.get("store_key")
    if not current_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store_row = conn.execute("SELECT * FROM stores WHERE access_key = ?", (current_key,)).fetchone()
    if not store_row:
        conn.close()
        return redirect(url_for("index"))
    store = dict(store_row)

    new_store_name = request.form.get("new_store_name", "").strip()
    custom_slug_input = request.form.get("new_store_slug", "").strip()

    if not new_store_name:
        conn.close()
        flash("Store Name cannot be empty.", "error")
        return redirect(url_for("dashboard"))

    desired_slug = create_slug(custom_slug_input if custom_slug_input else new_store_name)

    conflict = conn.execute(
        "SELECT id FROM stores WHERE store_slug = ? AND id != ?",
        (desired_slug, store["id"])
    ).fetchone()

    if conflict:
        conn.close()
        flash(f"The web link '/{desired_slug}' is already taken by another shop. Try adding your city or 'official'!", "error")
        return redirect(url_for("dashboard"))

    if desired_slug != store["store_slug"]:
        save_alias_if_needed(conn, store["id"], store["store_slug"], store["store_name"])
        conn.execute("DELETE FROM store_aliases WHERE old_slug = ? AND store_id = ?", (desired_slug, store["id"]))

    conn.execute(
        "UPDATE stores SET store_name = ?, store_slug = ? WHERE id = ?",
        (new_store_name, desired_slug, store["id"])
    )
    conn.commit()
    conn.close()

    flash(f"✨ Store renamed to '{new_store_name}'! Your new link is /{desired_slug}", "success")
    return redirect(url_for("dashboard"))


@app.route("/update-avatar", methods=["POST"])
def update_avatar():
    current_key = session.get("store_key")
    if not current_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store = conn.execute("SELECT id FROM stores WHERE access_key = ?", (current_key,)).fetchone()
    if not store:
        conn.close()
        return redirect(url_for("index"))

    compressed_logo = request.form.get("compressed_logo", "").strip()
    logo_file = request.files.get("logo_file")
    logo_data = compressed_logo if compressed_logo.startswith("data:image") else encode_uploaded_image(logo_file)

    if logo_data:
        conn.execute("UPDATE stores SET logo_data = ? WHERE id = ?", (logo_data, store["id"]))
        conn.commit()
        flash("📸 Profile picture updated on your live store!", "success")
    else:
        flash("Please choose an image file to upload.", "error")

    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/update-theme", methods=["POST"])
def update_theme():
    current_key = session.get("store_key")
    if not current_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store_row = conn.execute("SELECT * FROM stores WHERE access_key = ?", (current_key,)).fetchone()
    if not store_row:
        conn.close()
        return redirect(url_for("index"))
    store = dict(store_row)

    theme_color = request.form.get("theme_color", store.get("theme_color") or "emerald").strip()
    compressed_logo = request.form.get("compressed_logo", "").strip()
    compressed_bg = request.form.get("compressed_bg", "").strip()
    logo_file = request.files.get("logo_file")
    banner_file = request.files.get("banner_file")
    bg_image_file = request.files.get("bg_image_file")
    bg_preset_url = request.form.get("bg_preset_url", "").strip()

    clear_banner = request.form.get("clear_banner") == "1"
    clear_bg = request.form.get("clear_bg") == "1"

    new_logo = compressed_logo if compressed_logo.startswith("data:image") else encode_uploaded_image(logo_file)
    logo_data = new_logo or store.get("logo_data") or ""

    banner_data = "" if clear_banner else (encode_uploaded_image(banner_file) or store.get("banner_data") or "")

    uploaded_bg = compressed_bg if compressed_bg.startswith("data:image") else encode_uploaded_image(bg_image_file)
    if clear_bg:
        bg_image_data = ""
    elif uploaded_bg:
        bg_image_data = uploaded_bg
    elif bg_preset_url:
        bg_image_data = bg_preset_url
    else:
        bg_image_data = store.get("bg_image_data") or ""

    conn.execute(
        "UPDATE stores SET theme_color = ?, logo_data = ?, banner_data = ?, bg_image_data = ? WHERE id = ?",
        (theme_color, logo_data, banner_data, bg_image_data, store["id"])
    )
    conn.commit()
    conn.close()

    flash("🎨 Store theme, background image & profile picture updated!", "success")
    return redirect(url_for("dashboard"))


@app.route("/update-store", methods=["POST"])
def update_store():
    current_key = session.get("store_key")
    if not current_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store_row = conn.execute("SELECT * FROM stores WHERE access_key = ?", (current_key,)).fetchone()
    if not store_row:
        conn.close()
        return redirect(url_for("index"))
    store = dict(store_row)

    store_name = request.form.get("store_name", store["store_name"]).strip() or store["store_name"]
    custom_slug_raw = request.form.get("store_slug", "").strip()
    whatsapp_number = clean_international_whatsapp(request.form.get("whatsapp_number", store["whatsapp_number"]))
    category = request.form.get("category", store["category"]).strip()
    bio = request.form.get("bio", store["bio"]).strip()
    country = request.form.get("country", store.get("country") or "Global").strip()
    default_currency = request.form.get("default_currency", store.get("default_currency") or "USD").strip()
    payment_method_label = request.form.get("payment_method_label", store.get("payment_method_label") or "Mobile Money / Instant Transfer").strip()
    momo_number = request.form.get("momo_number", store.get("momo_number", "")).strip()
    momo_name = request.form.get("momo_name", store.get("momo_name", "")).strip()
    theme_color = request.form.get("theme_color", store.get("theme_color") or "emerald").strip()
    logo_file = request.files.get("logo_file")
    banner_file = request.files.get("banner_file")

    new_slug = create_slug(custom_slug_raw) if custom_slug_raw else create_slug(store_name)
    conflict = conn.execute(
        "SELECT id FROM stores WHERE store_slug = ? AND id != ?",
        (new_slug, store["id"])
    ).fetchone()
    if conflict:
        new_slug = store["store_slug"]
    elif new_slug != store["store_slug"]:
        save_alias_if_needed(conn, store["id"], store["store_slug"], store["store_name"])
        conn.execute("DELETE FROM store_aliases WHERE old_slug = ? AND store_id = ?", (new_slug, store["id"]))

    logo_data = encode_uploaded_image(logo_file) or store.get("logo_data") or ""
    banner_data = encode_uploaded_image(banner_file) or store.get("banner_data") or ""

    conn.execute(
        """UPDATE stores
           SET store_name = ?, store_slug = ?, whatsapp_number = ?, category = ?, bio = ?,
               logo_data = ?, banner_data = ?, theme_color = ?, country = ?,
               default_currency = ?, payment_method_label = ?, momo_number = ?, momo_name = ?
           WHERE id = ?""",
        (
            store_name, new_slug, whatsapp_number, category, bio,
            logo_data, banner_data, theme_color, country,
            default_currency, payment_method_label, momo_number, momo_name, store["id"]
        )
    )
    conn.commit()
    conn.close()

    flash("Store profile, global settings & payment details updated!", "success")
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    access_key = session.get("store_key")
    if not access_key:
        flash("Please enter your Store Secret Key to open your dashboard.", "error")
        return redirect(url_for("index"))

    conn = get_db_connection()
    store_row = conn.execute("SELECT * FROM stores WHERE access_key = ?", (access_key,)).fetchone()

    if not store_row:
        session.pop("store_key", None)
        conn.close()
        return redirect(url_for("index"))

    store = dict(store_row)
    raw_products = conn.execute(
        "SELECT * FROM products WHERE store_id = ? ORDER BY id DESC", (store["id"],)
    ).fetchall()
    conn.close()

    products = hydrate_products(raw_products)
    new_store_alert = session.pop("new_store_created", False)
    return render_template(
        "dashboard.html",
        store=store,
        products=products,
        new_store_alert=new_store_alert
    )


@app.route("/add-product", methods=["POST"])
def add_product():
    access_key = session.get("store_key")
    if not access_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store_row = conn.execute("SELECT id, default_currency FROM stores WHERE access_key = ?", (access_key,)).fetchone()
    if not store_row:
        conn.close()
        return redirect(url_for("index"))
    store = dict(store_row)

    product_name = request.form.get("product_name", "").strip()
    price = request.form.get("price", "0").strip()
    compare_price = request.form.get("compare_price", "0").strip()
    badge_text = request.form.get("badge_text", "").strip()
    variants_text = request.form.get("variants_text", "").strip()
    currency = request.form.get("currency", store.get("default_currency") or "USD").strip()
    description = request.form.get("description", "").strip()
    image_url = request.form.get("image_url", "").strip()
    compressed_images_json = request.form.get("compressed_images_json", "").strip()

    images_list = []

    # 1. Prefer fast client-compressed images if available
    if compressed_images_json:
        try:
            parsed_compressed = json.loads(compressed_images_json)
            if isinstance(parsed_compressed, list):
                images_list.extend([img for img in parsed_compressed if isinstance(img, str) and img.startswith("data:image")])
        except Exception:
            pass

    # 2. Fallback to standard file upload if no client-compressed array
    if not images_list:
        uploaded_files = request.files.getlist("image_files")
        for f in uploaded_files:
            encoded = encode_uploaded_image(f)
            if encoded:
                images_list.append(encoded)

    if image_url:
        for url_part in image_url.split(","):
            clean_url = url_part.strip()
            if clean_url:
                images_list.append(clean_url)

    if not images_list:
        images_list = ["https://placehold.co/600x600/0f172a/10b981?text=Varsell+Product"]

    primary_image = images_list[0]
    images_json_str = json.dumps(images_list)

    try:
        numeric_price = float(price)
    except ValueError:
        numeric_price = 0.0

    try:
        numeric_compare = float(compare_price) if compare_price else 0.0
    except ValueError:
        numeric_compare = 0.0

    conn.execute(
        """INSERT INTO products (
               store_id, product_name, price, compare_price, badge_text, variants_text,
               currency, description, image_data, images_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            store["id"], product_name, numeric_price, numeric_compare, badge_text, variants_text,
            currency, description, primary_image, images_json_str
        )
    )
    conn.commit()
    conn.close()

    flash(f"🚀 Published '{product_name}' with {len(images_list)} photo(s)!", "success")
    return redirect(url_for("dashboard"))


@app.route("/edit-product/<int:product_id>", methods=["POST"])
def edit_product(product_id):
    access_key = session.get("store_key")
    if not access_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store = conn.execute("SELECT id FROM stores WHERE access_key = ?", (access_key,)).fetchone()
    if not store:
        conn.close()
        return redirect(url_for("index"))

    product_row = conn.execute(
        "SELECT * FROM products WHERE id = ? AND store_id = ?",
        (product_id, store["id"])
    ).fetchone()

    if not product_row:
        conn.close()
        flash("Product not found.", "error")
        return redirect(url_for("dashboard"))

    product = hydrate_products([product_row])[0]

    new_name = request.form.get("product_name", product["product_name"]).strip() or product["product_name"]
    new_currency = request.form.get("currency", product["currency"]).strip() or product["currency"]
    new_description = request.form.get("description", product["description"]).strip()
    new_badge = request.form.get("badge_text", "").strip()
    new_variants = request.form.get("variants_text", "").strip()
    price_raw = request.form.get("price", str(product["price"])).strip()
    compare_raw = request.form.get("compare_price", "0").strip()
    photo_mode = request.form.get("photo_mode", "append").strip()
    new_image_url = request.form.get("image_url", "").strip()
    compressed_edit_json = request.form.get("compressed_edit_json", "").strip()

    try:
        new_price = float(price_raw)
    except ValueError:
        new_price = product["price"]

    try:
        new_compare = float(compare_raw) if compare_raw else 0.0
    except ValueError:
        new_compare = 0.0

    kept_indices = request.form.getlist("keep_image_idx")
    existing_images = []
    for idx_str in kept_indices:
        try:
            idx = int(idx_str)
            if 0 <= idx < len(product["images_list"]):
                existing_images.append(product["images_list"][idx])
        except ValueError:
            pass

    new_images = []
    if compressed_edit_json:
        try:
            parsed_edit = json.loads(compressed_edit_json)
            if isinstance(parsed_edit, list):
                new_images.extend([img for img in parsed_edit if isinstance(img, str) and img.startswith("data:image")])
        except Exception:
            pass

    if not new_images:
        uploaded_files = request.files.getlist("image_files")
        for f in uploaded_files:
            encoded = encode_uploaded_image(f)
            if encoded:
                new_images.append(encoded)

    if new_image_url:
        for url_part in new_image_url.split(","):
            clean_url = url_part.strip()
            if clean_url:
                new_images.append(clean_url)

    if new_images:
        if photo_mode == "replace":
            final_images = new_images
        else:
            final_images = existing_images + new_images
    else:
        final_images = existing_images if existing_images else product["images_list"]

    if not final_images:
        final_images = ["https://placehold.co/600x600/0f172a/10b981?text=Varsell+Product"]

    primary_image = final_images[0]
    images_json_str = json.dumps(final_images)

    conn.execute(
        """UPDATE products
           SET product_name = ?, price = ?, compare_price = ?, badge_text = ?, variants_text = ?,
               currency = ?, description = ?, image_data = ?, images_json = ?
           WHERE id = ? AND store_id = ?""",
        (
            new_name, new_price, new_compare, new_badge, new_variants,
            new_currency, new_description, primary_image, images_json_str,
            product_id, store["id"]
        )
    )
    conn.commit()
    conn.close()

    flash(f"✨ Updated '{new_name}'!", "success")
    return redirect(url_for("dashboard"))


@app.route("/toggle-stock/<int:product_id>", methods=["POST"])
def toggle_stock(product_id):
    access_key = session.get("store_key")
    if not access_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store = conn.execute("SELECT id FROM stores WHERE access_key = ?", (access_key,)).fetchone()
    if store:
        product = conn.execute(
            "SELECT in_stock FROM products WHERE id = ? AND store_id = ?",
            (product_id, store["id"])
        ).fetchone()
        if product:
            new_status = 0 if product["in_stock"] == 1 else 1
            conn.execute(
                "UPDATE products SET in_stock = ? WHERE id = ?", (new_status, product_id)
            )
            conn.commit()
            flash("Product stock status updated.", "success")
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/delete-product/<int:product_id>", methods=["POST"])
def delete_product(product_id):
    access_key = session.get("store_key")
    if not access_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store = conn.execute("SELECT id FROM stores WHERE access_key = ?", (access_key,)).fetchone()
    if store:
        conn.execute(
            "DELETE FROM products WHERE id = ? AND store_id = ?",
            (product_id, store["id"])
        )
        conn.commit()
        flash("Product removed from store.", "success")
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.pop("store_key", None)
    return redirect(url_for("index"))


# --- FOUNDER MASTER ADMIN ROUTES ---

@app.route("/admin", methods=["GET", "POST"])
def admin_panel():
    if request.method == "POST":
        entered_key = request.form.get("admin_key", "").strip()
        if entered_key.upper() == ADMIN_MASTER_KEY.upper() or normalize_key(entered_key) == normalize_key(ADMIN_MASTER_KEY):
            session.permanent = True
            session["is_varsell_admin"] = True
            flash("⚡ Welcome back, Founder! Master Command HQ unlocked.", "success")
            return redirect(url_for("admin_panel"))
        else:
            flash("Invalid Master Admin Key.", "error")
            return redirect(url_for("admin_panel"))

    is_admin = session.get("is_varsell_admin", False)
    if not is_admin:
        return render_template("admin.html", is_admin=False)

    conn = get_db_connection()
    stores = conn.execute("""
        SELECT s.*, COUNT(p.id) AS product_count
        FROM stores s
        LEFT JOIN products p ON s.id = p.store_id
        GROUP BY s.id
        ORDER BY s.id DESC
    """).fetchall()

    total_products_row = conn.execute("SELECT COUNT(*) AS cnt FROM products").fetchone()
    total_products = total_products_row["cnt"] if total_products_row else 0
    conn.close()

    db_type = "Neon Cloud PostgreSQL" if IS_POSTGRES else "Local SQLite"
    return render_template(
        "admin.html",
        is_admin=True,
        stores=stores,
        total_products=total_products,
        db_type=db_type,
        master_key=ADMIN_MASTER_KEY
    )


@app.route("/admin/manage-store/<int:store_id>")
def admin_manage_store(store_id):
    if not session.get("is_varsell_admin"):
        return redirect(url_for("admin_panel"))

    conn = get_db_connection()
    store = conn.execute("SELECT access_key, store_name FROM stores WHERE id = ?", (store_id,)).fetchone()
    conn.close()

    if store:
        session.permanent = True
        session["store_key"] = store["access_key"]
        flash(f"⚡ Founder Mode: You are now managing '{store['store_name']}'.", "success")
        return redirect(url_for("dashboard"))

    return redirect(url_for("admin_panel"))


@app.route("/admin/delete-store/<int:store_id>", methods=["POST"])
def admin_delete_store(store_id):
    if not session.get("is_varsell_admin"):
        return redirect(url_for("admin_panel"))

    conn = get_db_connection()
    conn.execute("DELETE FROM products WHERE store_id = ?", (store_id,))
    conn.execute("DELETE FROM store_aliases WHERE store_id = ?", (store_id,))
    conn.execute("DELETE FROM stores WHERE id = ?", (store_id,))
    conn.commit()
    conn.close()

    flash("Store and all its products permanently removed.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin-logout")
def admin_logout():
    session.pop("is_varsell_admin", None)
    return redirect(url_for("index"))


# --- SMART DYNAMIC STOREFRONT ROUTES ---

@app.route("/store/<store_slug>")
@app.route("/<store_slug>")
def view_store(store_slug):
    conn = get_db_connection()
    store, should_redirect = find_store_by_any_identifier(conn, store_slug)

    if not store:
        conn.close()
        return redirect(url_for("index", q=store_slug))

    if should_redirect and store["store_slug"] != store_slug:
        conn.close()
        return redirect(f"/{store['store_slug']}", code=302)

    raw_products = conn.execute(
        "SELECT * FROM products WHERE store_id = ? ORDER BY in_stock DESC, id DESC",
        (store["id"],)
    ).fetchall()
    conn.close()

    products = hydrate_products(raw_products)
    return render_template("store.html", store=store, products=products)


if __name__ == "__main__":
    app.run(debug=True)
