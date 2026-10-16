import os
import re
import sqlite3
import secrets
import string
import base64
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, jsonify

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "varsell-super-secret-dev-key-2026")
ADMIN_MASTER_KEY = os.environ.get("ADMIN_KEY", "VARSELL-BOSS-2026")

DB_NAME = "varsell.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_POSTGRES = bool(DATABASE_URL and psycopg2)

# Reserved system routes so a seller doesn't accidentally overwrite app routes
RESERVED_SLUGS = {
    "login", "logout", "dashboard", "admin", "admin-logout", "create-store",
    "change-key", "change-store-name", "update-store", "add-product",
    "manifest.json", "sw.js", "icon.svg", "sitemap.xml", "robots.txt", "store"
}


class DBWrapper:
    def __init__(self):
        if IS_POSTGRES:
            self.conn = psycopg2.connect(DATABASE_URL)
        else:
            self.conn = sqlite3.connect(DB_NAME)
            self.conn.row_factory = sqlite3.Row

    def execute(self, query, params=()):
        if IS_POSTGRES:
            pg_query = query.replace("?", "%s")
            cursor = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(pg_query, params)
            return cursor
        else:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            return cursor

    def commit(self):
        self.conn.commit()

    def close(self):
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
                momo_number TEXT DEFAULT '',
                momo_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS logo_data TEXT DEFAULT ''")
        conn.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS momo_number TEXT DEFAULT ''")
        conn.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS momo_name TEXT DEFAULT ''")

        # Alias table remembers every old store name/slug so old links never break
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
                currency TEXT DEFAULT 'GHS',
                description TEXT DEFAULT '',
                image_data TEXT NOT NULL,
                in_stock INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
                momo_number TEXT DEFAULT '',
                momo_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col in ["logo_data", "momo_number", "momo_name"]:
            try:
                conn.execute(f"ALTER TABLE stores ADD COLUMN {col} TEXT DEFAULT ''")
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
                currency TEXT DEFAULT 'GHS',
                description TEXT DEFAULT '',
                image_data TEXT NOT NULL,
                in_stock INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (store_id) REFERENCES stores (id) ON DELETE CASCADE
            )
        """)
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
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", store_name).strip().lower()
    slug = re.sub(r"[\s-]+", "-", slug)
    if not slug or slug in RESERVED_SLUGS:
        slug = f"{slug or 'shop'}-store"
    return slug


def clean_whatsapp_number(number):
    cleaned = re.sub(r"[^\d]", "", number)
    if len(cleaned) == 10 and cleaned.startswith("0"):
        cleaned = "233" + cleaned[1:]
    return cleaned


def save_alias_if_needed(conn, store_id, old_slug, old_name):
    """Saves previous slug & name in store_aliases so old links always redirect to the new name"""
    existing = conn.execute(
        "SELECT id FROM store_aliases WHERE old_slug = ?", (old_slug,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO store_aliases (store_id, old_slug, old_name) VALUES (?, ?, ?)",
            (store_id, old_slug, old_name)
        )


def find_store_by_any_identifier(conn, identifier):
    """
    Smart Store Resolver:
    1. Exact slug match
    2. Normalized slug match (e.g. 'Zayn Luxury' -> 'zayn-luxury')
    3. Case-insensitive Store Name match
    4. Compact/no-dash match (e.g. 'zaynluxury' matches 'zayn-luxury')
    5. Old slug/name alias match (redirects to newest store_slug)
    """
    raw = identifier.strip()
    slug_candidate = create_slug(raw)
    compact_candidate = re.sub(r"[^a-z0-9]", "", raw.lower())

    # 1. Exact or normalized slug match
    store = conn.execute(
        "SELECT * FROM stores WHERE store_slug = ? OR store_slug = ?",
        (raw.lower(), slug_candidate)
    ).fetchone()
    if store:
        return store, False

    # 2. Direct Store Name match (case-insensitive)
    store = conn.execute(
        "SELECT * FROM stores WHERE LOWER(store_name) = ?",
        (raw.lower(),)
    ).fetchone()
    if store:
        return store, True

    # 3. Check old aliases (if the user renamed their store earlier)
    alias = conn.execute(
        "SELECT store_id FROM store_aliases WHERE old_slug = ? OR old_slug = ? OR LOWER(old_name) = ?",
        (raw.lower(), slug_candidate, raw.lower())
    ).fetchone()
    if alias:
        store = conn.execute("SELECT * FROM stores WHERE id = ?", (alias["store_id"],)).fetchone()
        if store:
            return store, True

    # 4. Compact / fuzzy match across all stores (e.g. typing 'kofidrip' finds 'kofi-drip')
    if compact_candidate:
        all_stores = conn.execute("SELECT * FROM stores").fetchall()
        for s in all_stores:
            s_compact = re.sub(r"[^a-z0-9]", "", s["store_slug"].lower())
            name_compact = re.sub(r"[^a-z0-9]", "", s["store_name"].lower())
            if compact_candidate in (s_compact, name_compact):
                return s, True

    return None, False


# --- PWA, OFFLINE & GOOGLE SEO ROUTES ---

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
        "name": "Varsell — Cloud Stores & WhatsApp Commerce",
        "short_name": "Varsell",
        "description": "Launch and browse online stores even offline. Built for WhatsApp & MoMo commerce.",
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
const CACHE_NAME = 'varsell-offline-v3';
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
  if (event.request.method !== 'GET') return;

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
    lines = [
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {host}/sitemap.xml"
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    """Dynamically lists every store's latest custom link so Google indexes new store names fast"""
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
    if q:
        like_q = f"%{q.lower()}%"
        recent_stores = conn.execute(
            """SELECT store_name, store_slug, category, bio, logo_data, momo_name
               FROM stores
               WHERE LOWER(store_name) LIKE ? OR LOWER(store_slug) LIKE ? OR LOWER(category) LIKE ?
               ORDER BY id DESC LIMIT 24""",
            (like_q, like_q, like_q)
        ).fetchall()
    else:
        recent_stores = conn.execute(
            "SELECT store_name, store_slug, category, bio, logo_data, momo_name FROM stores ORDER BY id DESC LIMIT 12"
        ).fetchall()
    conn.close()
    return render_template("index.html", recent_stores=recent_stores, search_query=q)


@app.route("/create-store", methods=["POST"])
def create_store():
    store_name = request.form.get("store_name", "").strip()
    whatsapp_number = request.form.get("whatsapp_number", "").strip()
    custom_key_input = request.form.get("custom_key", "").strip()
    category = request.form.get("category", "General").strip()
    bio = request.form.get("bio", "").strip()
    momo_number = request.form.get("momo_number", "").strip()
    momo_name = request.form.get("momo_name", "").strip()

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
            flash(f"The Secret Key '{access_key}' is already taken. Please choose another one.", "error")
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

    formatted_wa = clean_whatsapp_number(whatsapp_number)

    conn.execute(
        """INSERT INTO stores (store_name, store_slug, access_key, whatsapp_number, category, bio, momo_number, momo_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (store_name, slug, access_key, formatted_wa, category, bio, momo_number, momo_name)
    )
    conn.commit()
    conn.close()

    session["store_key"] = access_key
    session["new_store_created"] = True
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["POST"])
def login():
    raw_input = request.form.get("access_key", "").strip()
    access_key = normalize_key(raw_input)

    if raw_input.upper() == ADMIN_MASTER_KEY.upper() or access_key == normalize_key(ADMIN_MASTER_KEY):
        session["is_varsell_admin"] = True
        flash("⚡ Founder Master Key recognized! Welcome to Varsell Command HQ.", "success")
        return redirect(url_for("admin_panel"))

    conn = get_db_connection()
    store = conn.execute("SELECT * FROM stores WHERE access_key = ?", (access_key,)).fetchone()
    conn.close()

    if store:
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

    session["store_key"] = new_key
    flash(f"Your Store Secret Key has been updated to: {new_key}", "success")
    return redirect(url_for("dashboard"))


@app.route("/change-store-name", methods=["POST"])
def change_store_name():
    """Allows a seller to customize their Store Name and Web Link to any name they want while keeping old links alive"""
    current_key = session.get("store_key")
    if not current_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store = conn.execute("SELECT * FROM stores WHERE access_key = ?", (current_key,)).fetchone()
    if not store:
        conn.close()
        return redirect(url_for("index"))

    new_store_name = request.form.get("new_store_name", "").strip()
    custom_slug_input = request.form.get("new_store_slug", "").strip()

    if not new_store_name:
        conn.close()
        flash("Store Name cannot be empty.", "error")
        return redirect(url_for("dashboard"))

    # Create new slug from custom_slug_input if provided, otherwise from the new store name
    desired_slug = create_slug(custom_slug_input if custom_slug_input else new_store_name)

    # Check if another store already owns this slug
    conflict = conn.execute(
        "SELECT id FROM stores WHERE store_slug = ? AND id != ?",
        (desired_slug, store["id"])
    ).fetchone()

    if conflict:
        conn.close()
        flash(f"The web link '/{desired_slug}' is already taken by another shop. Try adding 'gh', 'shop', or your city!", "error")
        return redirect(url_for("dashboard"))

    # Save the old slug/name in store_aliases so old links still redirect automatically
    if desired_slug != store["store_slug"]:
        save_alias_if_needed(conn, store["id"], store["store_slug"], store["store_name"])
        # Remove desired_slug from aliases if this store previously used it
        conn.execute("DELETE FROM store_aliases WHERE old_slug = ? AND store_id = ?", (desired_slug, store["id"]))

    conn.execute(
        "UPDATE stores SET store_name = ?, store_slug = ? WHERE id = ?",
        (new_store_name, desired_slug, store["id"])
    )
    conn.commit()
    conn.close()

    flash(f"✨ Store renamed to '{new_store_name}'! Your new link is /{desired_slug} (and your old link still redirects automatically!).", "success")
    return redirect(url_for("dashboard"))


@app.route("/update-store", methods=["POST"])
def update_store():
    current_key = session.get("store_key")
    if not current_key:
        return redirect(url_for("index"))

    conn = get_db_connection()
    store = conn.execute("SELECT * FROM stores WHERE access_key = ?", (current_key,)).fetchone()
    if not store:
        conn.close()
        return redirect(url_for("index"))

    store_name = request.form.get("store_name", store["store_name"]).strip() or store["store_name"]
    custom_slug_raw = request.form.get("store_slug", "").strip()
    whatsapp_number = clean_whatsapp_number(request.form.get("whatsapp_number", store["whatsapp_number"]))
    category = request.form.get("category", store["category"]).strip()
    bio = request.form.get("bio", store["bio"]).strip()
    momo_number = request.form.get("momo_number", store.get("momo_number", "")).strip()
    momo_name = request.form.get("momo_name", store.get("momo_name", "")).strip()
    logo_file = request.files.get("logo_file")

    # Determine if slug should update
    new_slug = create_slug(custom_slug_raw) if custom_slug_raw else create_slug(store_name)
    conflict = conn.execute(
        "SELECT id FROM stores WHERE store_slug = ? AND id != ?",
        (new_slug, store["id"])
    ).fetchone()
    if conflict:
        new_slug = store["store_slug"]  # Keep current slug if conflict
    elif new_slug != store["store_slug"]:
        save_alias_if_needed(conn, store["id"], store["store_slug"], store["store_name"])
        conn.execute("DELETE FROM store_aliases WHERE old_slug = ? AND store_id = ?", (new_slug, store["id"]))

    logo_data = store["logo_data"] or ""
    if logo_file and logo_file.filename != "":
        file_bytes = logo_file.read()
        mime_type = logo_file.mimetype or "image/jpeg"
        b64_encoded = base64.b64encode(file_bytes).decode("utf-8")
        logo_data = f"data:{mime_type};base64,{b64_encoded}"

    conn.execute(
        """UPDATE stores
           SET store_name = ?, store_slug = ?, whatsapp_number = ?, category = ?, bio = ?, logo_data = ?, momo_number = ?, momo_name = ?
           WHERE id = ?""",
        (store_name, new_slug, whatsapp_number, category, bio, logo_data, momo_number, momo_name, store["id"])
    )
    conn.commit()
    conn.close()

    flash("Store name, web link, MoMo details & branding updated!", "success")
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    access_key = session.get("store_key")
    if not access_key:
        flash("Please enter your Store Secret Key to open your dashboard.", "error")
        return redirect(url_for("index"))

    conn = get_db_connection()
    store = conn.execute("SELECT * FROM stores WHERE access_key = ?", (access_key,)).fetchone()

    if not store:
        session.pop("store_key", None)
        conn.close()
        return redirect(url_for("index"))

    products = conn.execute(
        "SELECT * FROM products WHERE store_id = ? ORDER BY id DESC", (store["id"],)
    ).fetchall()
    conn.close()

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
    store = conn.execute("SELECT id FROM stores WHERE access_key = ?", (access_key,)).fetchone()
    if not store:
        conn.close()
        return redirect(url_for("index"))

    product_name = request.form.get("product_name", "").strip()
    price = request.form.get("price", "0").strip()
    currency = request.form.get("currency", "GHS").strip()
    description = request.form.get("description", "").strip()
    image_url = request.form.get("image_url", "").strip()
    image_file = request.files.get("image_file")

    image_data = ""
    if image_file and image_file.filename != "":
        file_bytes = image_file.read()
        mime_type = image_file.mimetype or "image/jpeg"
        b64_encoded = base64.b64encode(file_bytes).decode("utf-8")
        image_data = f"data:{mime_type};base64,{b64_encoded}"
    elif image_url:
        image_data = image_url
    else:
        image_data = "https://placehold.co/600x600/0f172a/10b981?text=Varsell+Product"

    try:
        numeric_price = float(price)
    except ValueError:
        numeric_price = 0.0

    conn.execute(
        """INSERT INTO products (store_id, product_name, price, currency, description, image_data)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (store["id"], product_name, numeric_price, currency, description, image_data)
    )
    conn.commit()
    conn.close()

    flash("Product published to your live store!", "success")
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


# --- SMART DYNAMIC STOREFRONT ROUTES (SUPPORTS /<store_slug> AND /store/<store_slug>) ---

@app.route("/store/<store_slug>")
@app.route("/<store_slug>")
def view_store(store_slug):
    conn = get_db_connection()
    store, should_redirect = find_store_by_any_identifier(conn, store_slug)

    if not store:
        conn.close()
        # If someone typed an unknown name in the URL, redirect to homepage search for that name
        return redirect(url_for("index", q=store_slug))

    # If accessed via an old store name/slug or unformatted name, redirect cleanly to their current custom link
    if should_redirect and store["store_slug"] != store_slug:
        conn.close()
        return redirect(f"/{store['store_slug']}", code=302)

    products = conn.execute(
        "SELECT * FROM products WHERE store_id = ? ORDER BY in_stock DESC, id DESC",
        (store["id"],)
    ).fetchall()
    conn.close()

    return render_template("store.html", store=store, products=products)


if __name__ == "__main__":
    app.run(debug=True)
