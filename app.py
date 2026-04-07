from flask import Flask, render_template, request, jsonify, session, send_from_directory
import sqlite3, hashlib, os
from datetime import datetime
from functools import wraps
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'inventory_secret_key_2024')

# ── Database configuration ─────────────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL')
IS_PG = bool(DATABASE_URL)

if IS_PG:
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as e:
        raise RuntimeError(f"psycopg2 not installed but DATABASE_URL is set: {e}")

# On Vercel filesystem is read-only except /tmp
_ON_VERCEL = bool(os.environ.get('VERCEL') or os.environ.get('VERCEL_ENV'))

DB_PATH = '/tmp/inventory.db' if _ON_VERCEL else os.path.join(os.path.dirname(__file__), 'inventory.db')
LP_UPLOAD = '/tmp/lp_uploads' if _ON_VERCEL else os.path.join(os.path.dirname(__file__), 'lp_uploads')
os.makedirs(LP_UPLOAD, exist_ok=True)


# ── Thin dual-database wrapper ─────────────────────────────────────────────
class DB:
    def __init__(self):
        if IS_PG:
            self._c = psycopg2.connect(DATABASE_URL)
        else:
            self._c = sqlite3.connect(DB_PATH)
            self._c.row_factory = sqlite3.Row
        self.lastrowid = None

    def _q(self, sql):
        return sql.replace('?', '%s') if IS_PG else sql

    def run(self, sql, params=()):
        sql = self._q(sql)
        if IS_PG:
            with self._c.cursor() as cur:
                cur.execute(sql, params)
            self._c.commit()
        else:
            self._c.execute(sql, params)
            self._c.commit()

    def insert(self, sql, params=()):
        sql = self._q(sql)
        if IS_PG:
            if 'RETURNING' not in sql.upper():
                sql += ' RETURNING id'
            with self._c.cursor() as cur:
                cur.execute(sql, params)
                self.lastrowid = cur.fetchone()[0]
            self._c.commit()
        else:
            cur = self._c.execute(sql, params)
            self._c.commit()
            self.lastrowid = cur.lastrowid
        return self.lastrowid

    def all(self, sql, params=()):
        sql = self._q(sql)
        if IS_PG:
            with self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        else:
            return [dict(r) for r in self._c.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        sql = self._q(sql)
        if IS_PG:
            with self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                r = cur.fetchone()
                return dict(r) if r else None
        else:
            r = self._c.execute(sql, params).fetchone()
            return dict(r) if r else None

    def scalar(self, sql, params=()):
        sql = self._q(sql)
        if IS_PG:
            with self._c.cursor() as cur:
                cur.execute(sql, params)
                r = cur.fetchone()
                return r[0] if r else 0
        else:
            r = self._c.execute(sql, params).fetchone()
            return r[0] if r else 0

    def close(self):
        self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            try:
                self._c.commit()
            except Exception:
                pass
        else:
            try:
                self._c.rollback()
            except Exception:
                pass
        self.close()


# ── Schema ─────────────────────────────────────────────────────────────────
def init_db():
    pk = 'SERIAL PRIMARY KEY' if IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    with DB() as db:
        db.run(f'''CREATE TABLE IF NOT EXISTS users (
            id {pk}, username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL, role TEXT DEFAULT 'admin')''')

        db.run(f'''CREATE TABLE IF NOT EXISTS employees (
            id {pk}, name TEXT NOT NULL, email TEXT, phone TEXT,
            position TEXT, salary REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

        db.run(f'''CREATE TABLE IF NOT EXISTS suppliers (
            id {pk}, name TEXT NOT NULL, contact TEXT, email TEXT,
            phone TEXT, address TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

        db.run(f'''CREATE TABLE IF NOT EXISTS categories (
            id {pk}, name TEXT NOT NULL, description TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

        db.run(f'''CREATE TABLE IF NOT EXISTS products (
            id {pk}, name TEXT NOT NULL, category_id INTEGER,
            supplier_id INTEGER, price REAL DEFAULT 0,
            quantity INTEGER DEFAULT 0, description TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

        db.run(f'''CREATE TABLE IF NOT EXISTS sales (
            id {pk}, product_id INTEGER, quantity INTEGER DEFAULT 1,
            unit_price REAL DEFAULT 0, total_price REAL DEFAULT 0,
            sale_date TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

        db.run(f'''CREATE TABLE IF NOT EXISTS shipments (
            id {pk}, date TEXT, awb_no TEXT UNIQUE, cost REAL,
            status TEXT, awb_file TEXT, invoice_file TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

        if IS_PG:
            db.run("CREATE UNIQUE INDEX IF NOT EXISTS idx_awb_no ON shipments(awb_no)")
        else:
            try:
                db.run("CREATE UNIQUE INDEX IF NOT EXISTS idx_awb_no ON shipments(awb_no)")
            except Exception:
                pass

        # ── default admin ──
        pw = hashlib.sha256('admin123'.encode()).hexdigest()
        if IS_PG:
            db.run("INSERT INTO users (username, password, role) VALUES (?,?,?) ON CONFLICT(username) DO NOTHING", ('admin', pw, 'admin'))
        else:
            db.run("INSERT OR IGNORE INTO users (username, password, role) VALUES (?,?,?)", ('admin', pw, 'admin'))

        # ── sample employees ──
        for name, email, phone, pos, sal in [
            ('Alice Johnson', 'alice@example.com', '555-0101', 'Manager', 5500),
            ('Bob Smith', 'bob@example.com', '555-0102', 'Warehouse Staff', 2800),
            ('Carol White', 'carol@example.com', '555-0103', 'Sales Rep', 3200),
            ('David Lee', 'david@example.com', '555-0104', 'Accountant', 4000),
            ('Eva Brown', 'eva@example.com', '555-0105', 'IT Support', 3800),
            ('Frank Davis', 'frank@example.com', '555-0106', 'Driver', 2500),
            ('Grace Kim', 'grace@example.com', '555-0107', 'Clerk', 2600),
        ]:
            if not db.scalar("SELECT COUNT(*) FROM employees WHERE name=?", (name,)):
                db.insert("INSERT INTO employees (name,email,phone,position,salary) VALUES (?,?,?,?,?)",
                          (name, email, phone, pos, sal))

        # ── sample suppliers ──
        for name, contact, email, phone, addr in [
            ('TechSupply Co', 'Tom Brown', 'tech@supply.com', '555-1001', '123 Tech Ave'),
            ('Global Goods Ltd', 'Sara Jones', 'sara@global.com', '555-1002', '456 Trade St'),
            ('FastParts Inc', 'Mike Ross', 'mike@fastparts.com', '555-1003', '789 Parts Blvd'),
            ('Quality Wholesale', 'Nancy Drew', 'nancy@qw.com', '555-1004', '321 Quality Rd'),
            ('Prime Distributors', 'James Bond', 'james@prime.com', '555-1005', '654 Prime Way'),
        ]:
            if not db.scalar("SELECT COUNT(*) FROM suppliers WHERE name=?", (name,)):
                db.insert("INSERT INTO suppliers (name,contact,email,phone,address) VALUES (?,?,?,?,?)",
                          (name, contact, email, phone, addr))

        # ── sample categories ──
        for name, desc in [
            ('Electronics', 'Electronic devices and accessories'),
            ('Furniture', 'Office and home furniture'),
            ('Stationery', 'Office supplies and stationery'),
            ('Clothing', 'Apparel and accessories'),
            ('Food & Beverage', 'Food items and drinks'),
        ]:
            if not db.scalar("SELECT COUNT(*) FROM categories WHERE name=?", (name,)):
                db.insert("INSERT INTO categories (name,description) VALUES (?,?)", (name, desc))


init_db()


# ── Auth helpers ───────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


# ── Routes ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/login', methods=['POST'])
def login():
    d = request.json
    pw = hashlib.sha256(d.get('password', '').encode()).hexdigest()
    with DB() as db:
        user = db.one("SELECT * FROM users WHERE username=? AND password=?",
                      (d.get('username', ''), pw))
    if user:
        session['user'] = user['username']
        session['role'] = user['role']
        return jsonify({'success': True, 'username': user['username'], 'role': user['role']})
    return jsonify({'success': False, 'error': 'Invalid username or password'}), 401


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/session')
def check_session():
    if 'user' in session:
        return jsonify({'logged_in': True, 'username': session['user'], 'role': session.get('role', 'admin')})
    return jsonify({'logged_in': False})


@app.route('/api/dashboard')
@login_required
def dashboard():
    with DB() as db:
        return jsonify({
            'employees':  db.scalar("SELECT COUNT(*) FROM employees"),
            'suppliers':  db.scalar("SELECT COUNT(*) FROM suppliers"),
            'categories': db.scalar("SELECT COUNT(*) FROM categories"),
            'products':   db.scalar("SELECT COUNT(*) FROM products"),
            'sales':      db.scalar("SELECT COUNT(*) FROM sales"),
            'shipments':  db.scalar("SELECT COUNT(*) FROM shipments"),
        })


# ── EMPLOYEES ─────────────────────────────────────────────────────────────
@app.route('/api/employees', methods=['GET'])
@login_required
def get_employees():
    with DB() as db:
        return jsonify(db.all("SELECT * FROM employees ORDER BY id DESC"))


@app.route('/api/employees', methods=['POST'])
@login_required
def add_employee():
    d = request.json
    with DB() as db:
        new_id = db.insert("INSERT INTO employees (name,email,phone,position,salary) VALUES (?,?,?,?,?)",
                           (d['name'], d.get('email',''), d.get('phone',''), d.get('position',''), d.get('salary',0)))
        row = db.one("SELECT * FROM employees WHERE id=?", (new_id,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/employees/<int:eid>', methods=['PUT'])
@login_required
def update_employee(eid):
    d = request.json
    with DB() as db:
        db.run("UPDATE employees SET name=?,email=?,phone=?,position=?,salary=? WHERE id=?",
               (d['name'], d.get('email',''), d.get('phone',''), d.get('position',''), d.get('salary',0), eid))
        row = db.one("SELECT * FROM employees WHERE id=?", (eid,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/employees/<int:eid>', methods=['DELETE'])
@login_required
def delete_employee(eid):
    with DB() as db:
        db.run("DELETE FROM employees WHERE id=?", (eid,))
    return jsonify({'success': True})


# ── SUPPLIERS ─────────────────────────────────────────────────────────────
@app.route('/api/suppliers', methods=['GET'])
@login_required
def get_suppliers():
    with DB() as db:
        return jsonify(db.all("SELECT * FROM suppliers ORDER BY id DESC"))


@app.route('/api/suppliers', methods=['POST'])
@login_required
def add_supplier():
    d = request.json
    with DB() as db:
        new_id = db.insert("INSERT INTO suppliers (name,contact,email,phone,address) VALUES (?,?,?,?,?)",
                           (d['name'], d.get('contact',''), d.get('email',''), d.get('phone',''), d.get('address','')))
        row = db.one("SELECT * FROM suppliers WHERE id=?", (new_id,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/suppliers/<int:sid>', methods=['PUT'])
@login_required
def update_supplier(sid):
    d = request.json
    with DB() as db:
        db.run("UPDATE suppliers SET name=?,contact=?,email=?,phone=?,address=? WHERE id=?",
               (d['name'], d.get('contact',''), d.get('email',''), d.get('phone',''), d.get('address',''), sid))
        row = db.one("SELECT * FROM suppliers WHERE id=?", (sid,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/suppliers/<int:sid>', methods=['DELETE'])
@login_required
def delete_supplier(sid):
    with DB() as db:
        db.run("DELETE FROM suppliers WHERE id=?", (sid,))
    return jsonify({'success': True})


# ── CATEGORIES ────────────────────────────────────────────────────────────
@app.route('/api/categories', methods=['GET'])
@login_required
def get_categories():
    with DB() as db:
        return jsonify(db.all("SELECT * FROM categories ORDER BY id DESC"))


@app.route('/api/categories', methods=['POST'])
@login_required
def add_category():
    d = request.json
    with DB() as db:
        new_id = db.insert("INSERT INTO categories (name,description) VALUES (?,?)",
                           (d['name'], d.get('description','')))
        row = db.one("SELECT * FROM categories WHERE id=?", (new_id,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/categories/<int:cid>', methods=['PUT'])
@login_required
def update_category(cid):
    d = request.json
    with DB() as db:
        db.run("UPDATE categories SET name=?,description=? WHERE id=?",
               (d['name'], d.get('description',''), cid))
        row = db.one("SELECT * FROM categories WHERE id=?", (cid,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/categories/<int:cid>', methods=['DELETE'])
@login_required
def delete_category(cid):
    with DB() as db:
        db.run("DELETE FROM categories WHERE id=?", (cid,))
    return jsonify({'success': True})


# ── PRODUCTS ──────────────────────────────────────────────────────────────
_PROD_JOIN = """
    SELECT p.*, c.name AS category_name, s.name AS supplier_name
    FROM products p
    LEFT JOIN categories c ON p.category_id = c.id
    LEFT JOIN suppliers  s ON p.supplier_id  = s.id"""


@app.route('/api/products', methods=['GET'])
@login_required
def get_products():
    with DB() as db:
        return jsonify(db.all(_PROD_JOIN + " ORDER BY p.id DESC"))


@app.route('/api/products', methods=['POST'])
@login_required
def add_product():
    d = request.json
    with DB() as db:
        new_id = db.insert("INSERT INTO products (name,category_id,supplier_id,price,quantity,description) VALUES (?,?,?,?,?,?)",
                           (d['name'], d.get('category_id'), d.get('supplier_id'),
                            d.get('price',0), d.get('quantity',0), d.get('description','')))
        row = db.one(_PROD_JOIN + " WHERE p.id=?", (new_id,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/products/<int:pid>', methods=['PUT'])
@login_required
def update_product(pid):
    d = request.json
    with DB() as db:
        db.run("UPDATE products SET name=?,category_id=?,supplier_id=?,price=?,quantity=?,description=? WHERE id=?",
               (d['name'], d.get('category_id'), d.get('supplier_id'),
                d.get('price',0), d.get('quantity',0), d.get('description',''), pid))
        row = db.one(_PROD_JOIN + " WHERE p.id=?", (pid,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/products/<int:pid>', methods=['DELETE'])
@login_required
def delete_product(pid):
    with DB() as db:
        db.run("DELETE FROM products WHERE id=?", (pid,))
    return jsonify({'success': True})


# ── SALES ─────────────────────────────────────────────────────────────────
_SALE_JOIN = """
    SELECT s.*, p.name AS product_name
    FROM sales s LEFT JOIN products p ON s.product_id = p.id"""


@app.route('/api/sales', methods=['GET'])
@login_required
def get_sales():
    with DB() as db:
        return jsonify(db.all(_SALE_JOIN + " ORDER BY s.id DESC"))


@app.route('/api/sales', methods=['POST'])
@login_required
def add_sale():
    d = request.json
    with DB() as db:
        product = db.one("SELECT price, quantity FROM products WHERE id=?", (d.get('product_id'),))
        qty = int(d.get('quantity', 1))
        unit_price = product['price'] if product else 0
        total = qty * unit_price
        sale_date = d.get('sale_date', datetime.now().strftime('%Y-%m-%d'))
        new_id = db.insert("INSERT INTO sales (product_id,quantity,unit_price,total_price,sale_date) VALUES (?,?,?,?,?)",
                           (d.get('product_id'), qty, unit_price, total, sale_date))
        if product:
            db.run("UPDATE products SET quantity = quantity - ? WHERE id=? AND quantity >= ?",
                   (qty, d.get('product_id'), qty))
        row = db.one(_SALE_JOIN + " WHERE s.id=?", (new_id,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/sales/<int:sid>', methods=['PUT'])
@login_required
def update_sale(sid):
    d = request.json
    with DB() as db:
        product = db.one("SELECT price FROM products WHERE id=?", (d.get('product_id'),))
        qty = int(d.get('quantity', 1))
        unit_price = product['price'] if product else 0
        total = qty * unit_price
        sale_date = d.get('sale_date', datetime.now().strftime('%Y-%m-%d'))
        db.run("UPDATE sales SET product_id=?,quantity=?,unit_price=?,total_price=?,sale_date=? WHERE id=?",
               (d.get('product_id'), qty, unit_price, total, sale_date, sid))
        row = db.one(_SALE_JOIN + " WHERE s.id=?", (sid,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/sales/<int:sid>', methods=['DELETE'])
@login_required
def delete_sale(sid):
    with DB() as db:
        db.run("DELETE FROM sales WHERE id=?", (sid,))
    return jsonify({'success': True})


# ── LOGISTIC PRO ──────────────────────────────────────────────────────────
def lp_save_file(key):
    f = request.files.get(key)
    if f and f.filename:
        fn = secure_filename(f.filename)
        f.save(os.path.join(LP_UPLOAD, fn))
        return fn
    return None


@app.route('/lp_uploads/<filename>')
@login_required
def lp_uploads(filename):
    return send_from_directory(LP_UPLOAD, filename)


@app.route('/api/lp/dashboard')
@login_required
def lp_dashboard():
    # SUBSTR(date, 1, 7) works in both SQLite and PostgreSQL for 'YYYY-MM-DD' strings
    month_sql = ("SELECT SUBSTR(date, 1, 7) as month, COUNT(*) as count "
                 "FROM shipments WHERE date IS NOT NULL "
                 "GROUP BY SUBSTR(date, 1, 7) ORDER BY SUBSTR(date, 1, 7)")
    with DB() as db:
        return jsonify({
            'total':     db.scalar("SELECT COUNT(*) FROM shipments"),
            'transit':   db.scalar("SELECT COUNT(*) FROM shipments WHERE status='Transit'"),
            'delivered': db.scalar("SELECT COUNT(*) FROM shipments WHERE status='Delivered'"),
            'returned':  db.scalar("SELECT COUNT(*) FROM shipments WHERE status='Returned'"),
            'monthly':   db.all(month_sql),
        })


@app.route('/api/lp/shipments', methods=['GET'])
@login_required
def lp_get_shipments():
    with DB() as db:
        return jsonify(db.all("SELECT * FROM shipments ORDER BY created_at DESC"))


@app.route('/api/lp/shipments', methods=['POST'])
@login_required
def lp_add_shipment():
    awb_file = lp_save_file('awb_file')
    invoice_file = lp_save_file('invoice_file')
    d = request.form
    awb_no = d.get('awb_no', '').strip()
    with DB() as db:
        if db.scalar("SELECT COUNT(*) FROM shipments WHERE awb_no=?", (awb_no,)):
            return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
        new_id = db.insert(
            "INSERT INTO shipments (date,awb_no,cost,status,awb_file,invoice_file) VALUES (?,?,?,?,?,?)",
            (d.get('date'), awb_no, d.get('cost') or None, d.get('status'), awb_file, invoice_file))
        row = db.one("SELECT * FROM shipments WHERE id=?", (new_id,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/lp/shipments/<int:sid>', methods=['PUT'])
@login_required
def lp_update_shipment(sid):
    awb_file = lp_save_file('awb_file')
    invoice_file = lp_save_file('invoice_file')
    d = request.form
    awb_no = d.get('awb_no', '').strip()
    with DB() as db:
        if not db.one("SELECT id FROM shipments WHERE id=?", (sid,)):
            return jsonify({'success': False, 'error': 'Not found'}), 404
        if db.scalar("SELECT COUNT(*) FROM shipments WHERE awb_no=? AND id!=?", (awb_no, sid)):
            return jsonify({'success': False, 'error': 'AWB No. already exists'}), 409
        db.run("""UPDATE shipments SET date=?,awb_no=?,cost=?,status=?,
                  awb_file=COALESCE(?,awb_file),invoice_file=COALESCE(?,invoice_file) WHERE id=?""",
               (d.get('date'), awb_no, d.get('cost') or None, d.get('status'), awb_file, invoice_file, sid))
        row = db.one("SELECT * FROM shipments WHERE id=?", (sid,))
    return jsonify({'success': True, 'record': row})


@app.route('/api/lp/shipments/<int:sid>', methods=['DELETE'])
@login_required
def lp_delete_shipment(sid):
    with DB() as db:
        db.run("DELETE FROM shipments WHERE id=?", (sid,))
    return jsonify({'success': True})


if __name__ == '__main__':
    app.run(debug=True, port=5001)
