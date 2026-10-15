import os
import re
import sqlite3
import secrets
import string
import base64
from flask import Flask, render_template, request, redirect, url_for, session, flash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "varsell-super-secret-dev-key-2026")
DB_NAME = "varsell.db"


def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name TEXT NOT NULL,
            store_slug TEXT UNIQUE NOT NULL,
            access_key TEXT UNIQUE NOT NULL,
            whatsapp_number TEXT NOT NULL,
            category TEXT DEFAULT 'General',
            bio TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
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
    """Fallback simple key generator if a user leaves the custom key blank"""
    digits = "".join(secrets.choice(string.digits) for _ in range(4))
    return f"VS-{digits}"


def normalize_key(raw_key):
    """Cleans a user's custom key (uppercase, spaces turned into dashes)"""
    cleaned = re.sub(r"\s+", "-", raw_key.strip().upper())
    cleaned = re.sub(r"[^A-Z0-9_-]", "", cleaned)
    return cleaned


def create_slug(store_name):
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", store_name).strip().lower()
    slug = re.sub(r"[\s-]+", "-", slug)
    return slug or "store"


def clean_whatsapp_number(number):
    cleaned = re.sub(r"[^\d]", "", number)
    if len(cleaned) == 10 and cleaned.startswith("0"):
        cleaned = "233" + cleaned[1:]
    return cleaned


@app.route("/")
def index():
    conn = get_db_connection()
    recent_stores = conn.execute(
        "SELECT store_name, store_slug, category, bio FROM stores ORDER BY id DESC LIMIT 6"
    ).fetchall()
    conn.close()
    return render_template("index.html", recent_stores=recent_stores)


@app.route("/create-store", methods=["POST"])
def create_store():
    store_name = request.form.get("store_name", "").strip()
    whatsapp_number = request.form.get("whatsapp_number", "").strip()
    custom_key_input = request.form.get("custom_key", "").strip()
    category = request.form.get("category", "General").strip()
    bio = request.form.get("bio", "").strip()

    if not store_name or not whatsapp_number:
        flash("Please provide both your Store Name and WhatsApp number.", "error")
        return redirect(url_for("index"))

    conn = get_db_connection()

    # Handle Custom Secret Key vs Auto-Generated Key
    if custom_key_input:
        access_key = normalize_key(custom_key_input)
        if len(access_key) < 4:
            conn.close()
            flash("Your custom Secret Key must be at least 4 characters long.", "error")
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

    # Generate unique URL slug
    base_slug = create_slug(store_name)
    slug = base_slug
    counter = 1
    while conn.execute("SELECT id FROM stores WHERE store_slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{counter}"
        counter += 1

    formatted_wa = clean_whatsapp_number(whatsapp_number)

    conn.execute(
        """INSERT INTO stores (store_name, store_slug, access_key, whatsapp_number, category, bio)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (store_name, slug, access_key, formatted_wa, category, bio)
    )
    conn.commit()
    conn.close()

    session["store_key"] = access_key
    session["new_store_created"] = True
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["POST"])
def login():
    access_key = normalize_key(request.form.get("access_key", ""))
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
        flash("Your new Secret Key must be at least 4 characters long (letters or numbers).", "error")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    store = conn.execute("SELECT id FROM stores WHERE access_key = ?", (current_key,)).fetchone()
    if not store:
        conn.close()
        return redirect(url_for("index"))

    # Make sure no other store is using this key
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


@app.route("/store/<store_slug>")
def view_store(store_slug):
    conn = get_db_connection()
    store = conn.execute("SELECT * FROM stores WHERE store_slug = ?", (store_slug,)).fetchone()

    if not store:
        conn.close()
        return "Store not found", 404

    products = conn.execute(
        "SELECT * FROM products WHERE store_id = ? ORDER BY in_stock DESC, id DESC",
        (store["id"],)
    ).fetchall()
    conn.close()

    return render_template("store.html", store=store, products=products)


if __name__ == "__main__":
    app.run(debug=True)