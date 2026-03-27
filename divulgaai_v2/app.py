
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import requests, re, sqlite3, hashlib, os, uuid, json
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "app_data.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_BOT_TOKEN = os.getenv("BOT_TOKEN", "COLOQUE_SEU_TOKEN_NO_RENDER")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@SEU_CANAL")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "troque-esta-chave")
DEFAULT_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}
PRICE_PATTERNS = [r'R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}', r'\d{1,3}(?:\.\d{3})*,\d{2}']

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE, display_name TEXT, password_hash TEXT, role TEXT,
        is_active INTEGER DEFAULT 1, created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_key TEXT, title TEXT, store TEXT, url TEXT,
        price_value REAL, price_text TEXT, image TEXT, coupon TEXT,
        created_by TEXT, created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS send_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, store TEXT, price_text TEXT, url TEXT, image TEXT,
        caption TEXT, sent_by TEXT, sent_at TEXT, source_status TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS post_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payload_json TEXT NOT NULL, caption TEXT NOT NULL, image TEXT,
        created_by TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
        reviewer TEXT, review_note TEXT, scheduled_for TEXT,
        commission_estimate TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS approval_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER, action TEXT, actor TEXT, note TEXT, created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS internal_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT, target_roles TEXT, is_read INTEGER DEFAULT 0, created_at TEXT)""")
    conn.commit()
    row = conn.execute("SELECT id FROM users WHERE username=?", (DEFAULT_ADMIN_USERNAME,)).fetchone()
    if not row:
        conn.execute("INSERT INTO users (username,display_name,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                     (DEFAULT_ADMIN_USERNAME, "Administrador", generate_password_hash(DEFAULT_ADMIN_PASSWORD), "admin", now_str()))
        conn.commit()
    conn.close()

def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrap(*a, **k):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Faça login novamente."}), 401
            return redirect(url_for("login"))
        return fn(*a, **k)
    return wrap

def current_role(): return session.get("role","")
def current_username(): return session.get("display_name") or session.get("username") or "Equipe"
def can_manage_users(): return current_role() in ("admin","subadmin")
def can_review(): return current_role() in ("admin","subadmin")
def is_admin(): return current_role() == "admin"

def clean_text(text):
    if not text: return ""
    return re.sub(r"\s+"," ",str(text)).strip()

def clean_multiline_text(text):
    if not text: return ""
    text = str(text).replace('\r\n', '\n').replace('\r', '\n')
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split('\n')]
    cleaned = '\n'.join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

def normalize_price(price):
    if not price: return ""
    price = clean_text(price)
    if not price.startswith("R$"): price = f"R$ {price}"
    return price

def price_to_float(price):
    if not price: return None
    try:
        return float(str(price).replace("R$","").replace(".","").replace(",",".").strip())
    except: return None

def get_meta(soup,*names):
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"): return clean_text(tag["content"])
    return ""

def domain_name(url):
    try:
        return urlparse(url).netloc.lower().replace("www.","").split(":")[0]
    except: return ""

def infer_store(url):
    first = domain_name(url).split(".")[0]
    map_ = {"mercadolivre":"Mercado Livre","amazon":"Amazon","shopee":"Shopee"}
    return map_.get(first, first.capitalize() if first else "Loja")

def choose_best_price(text):
    prices = []
    for p in PRICE_PATTERNS:
        prices.extend(re.findall(p, text, re.I))
    vals = []
    for p in prices:
        n = normalize_price(p)
        v = price_to_float(n)
        if v and 1 <= v <= 100000:
            vals.append((n,v))
    vals.sort(key=lambda x:x[1])
    return vals[0][0] if vals else ""

def build_key(url,title,image):
    base = f"{domain_name(url)}|{clean_text(title).lower()[:120]}|{str(image)[:120]}".encode()
    return hashlib.sha1(base).hexdigest()

def get_history_info(product_key):
    conn = db()
    rows = conn.execute("SELECT price_value, price_text FROM price_history WHERE product_key=? AND price_value IS NOT NULL ORDER BY price_value ASC, id ASC",
                        (product_key,)).fetchall()
    conn.close()
    best = rows[0] if rows else None
    ref = best
    suspicious = ""
    if len(rows) >= 2:
        low, second = rows[0], rows[1]
        if low["price_value"] and second["price_value"] and low["price_value"] <= second["price_value"] * 0.7:
            ref = second
            suspicious = low["price_text"] or ""
    return {
        "best_price_text": best["price_text"] if best else "",
        "reference_price_value": ref["price_value"] if ref else None,
        "reference_price_text": ref["price_text"] if ref else "",
        "suspicious_low_text": suspicious,
    }

def classify_fire(price_value, ref):
    if not price_value: return {"emoji":"🔥","label":"Preço ok"}
    if ref:
        ratio = price_value / ref
        if ratio <= 1.00: return {"emoji":"🔥🔥🔥","label":"Melhor preço até agora"}
        if ratio <= 1.05: return {"emoji":"🔥🔥","label":"Preço muito bom"}
    return {"emoji":"🔥","label":"Preço ok"}

def default_headline(product):
    fire = product.get("fire_emoji") or "🔥"
    label = product.get("fire_label") or "Preço ok"
    store = (product.get("store") or "Loja").upper()
    return f"{fire} {label.upper()} NESSE {store}"

def generate_copy(p):
    lines = [p.get("headline") or default_headline(p), "", p.get("title") or "Oferta do dia"]
    lines.append(f"De {p.get('old_price')} por {p.get('price')}" if p.get("old_price") else f"Por {p.get('price') or 'R$ 0,00'}")
    if p.get("coupon"): lines.append(f"Cupom: {p['coupon']} 🎟️")
    lines += ["", f"Loja Oficial {p.get('store') or 'Loja'}"]
    if p.get("url"): lines.append(p["url"])
    if p.get("best_price_text"): lines += ["", f"Referência de histórico: {p['best_price_text']}"]
    if p.get("disclaimer"): lines += ["", p["disclaimer"]]
    return "\n".join(lines)

def enrich(product, preserve=True):
    if not product.get("product_key"):
        product["product_key"] = build_key(product.get("url",""), product.get("title",""), product.get("image",""))
    product["price"] = normalize_price(product.get("price",""))
    product["price_value"] = price_to_float(product.get("price",""))
    hist = get_history_info(product["product_key"])
    fire = classify_fire(product["price_value"], hist["reference_price_value"])
    product["fire_emoji"] = fire["emoji"]
    if not preserve or not product.get("fire_label"): product["fire_label"] = fire["label"]
    product["best_price_text"] = hist["reference_price_text"]
    product["suspicious_low_text"] = hist["suspicious_low_text"]
    if not preserve or not product.get("headline"): product["headline"] = default_headline(product)
    product["copy"] = generate_copy(product)
    return product

def fetch_product(url):
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=20, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    title = get_meta(soup, "og:title", "twitter:title") or clean_text(soup.title.text if soup.title else "") or "Produto encontrado"
    raw = clean_text(soup.get_text(" ", strip=True))
    image = get_meta(soup, "og:image", "twitter:image")
    store = infer_store(r.url or url)
    product = {
        "url": url, "title": title[:250], "store": store,
        "price": choose_best_price(raw), "old_price": "", "coupon": "",
        "image": image, "disclaimer": "", "headline": "", "internal_comment": "",
        "commission_estimate": "", "product_key": build_key(r.url or url, title, image)
    }
    return enrich(product, preserve=False)

def send_to_telegram(caption, image):
    if not DEFAULT_BOT_TOKEN or "COLOQUE_SEU_TOKEN" in DEFAULT_BOT_TOKEN:
        raise RuntimeError("Configure BOT_TOKEN e TELEGRAM_CHAT_ID no Render.")
    base = f"https://api.telegram.org/bot{DEFAULT_BOT_TOKEN}"
    if image:
        r = requests.post(base + "/sendPhoto", data={"chat_id": DEFAULT_CHAT_ID, "photo": image, "caption": caption[:1024]}, timeout=25)
        if r.ok: return r.json()
    r = requests.post(base + "/sendMessage", data={"chat_id": DEFAULT_CHAT_ID, "text": caption}, timeout=25)
    r.raise_for_status()
    return r.json()

def add_notification(message, target="admin,subadmin"):
    conn = db()
    conn.execute("INSERT INTO internal_notifications (message,target_roles,created_at) VALUES (?,?,?)",
                 (message[:300], target, now_str()))
    conn.commit(); conn.close()

def add_approval(post_id, action, note=""):
    conn = db()
    conn.execute("INSERT INTO approval_history (post_id,action,actor,note,created_at) VALUES (?,?,?,?,?)",
                 (post_id, action, current_username(), note[:500], now_str()))
    conn.commit(); conn.close()

def recent_posts(limit=15):
    conn = db()
    rows = conn.execute("SELECT id,title,store,price_text,sent_by,sent_at,source_status FROM send_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def team_users():
    conn = db()
    rows = conn.execute("SELECT id,username,display_name,role,is_active,created_at FROM users ORDER BY username").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def pending_posts():
    conn = db()
    rows = conn.execute("""SELECT id,caption,created_by,status,review_note,scheduled_for,created_at,commission_estimate
                           FROM post_queue WHERE status IN ('pending_review','approved_scheduled','draft','rejected','error')
                           ORDER BY id DESC LIMIT 50""").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def drafts():
    conn = db()
    rows = conn.execute("SELECT id,created_by,created_at,updated_at,scheduled_for,commission_estimate,payload_json FROM post_queue WHERE status='draft' ORDER BY id DESC LIMIT 50").fetchall()
    out = []
    for r in rows:
        try: payload = json.loads(r["payload_json"])
        except: payload = {}
        out.append({
            "id": r["id"], "created_by": r["created_by"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            "scheduled_for": r["scheduled_for"], "commission_estimate": r["commission_estimate"],
            "title": payload.get("title",""), "price": payload.get("price",""), "store": payload.get("store","")
        })
    conn.close()
    return out

def approval_history(limit=50):
    conn = db()
    rows = conn.execute("SELECT id,post_id,action,actor,note,created_at FROM approval_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def dashboard_numbers():
    conn = db()
    nums = {
        "total_users": conn.execute("SELECT COUNT(*) c FROM users WHERE is_active=1").fetchone()["c"],
        "total_sent": conn.execute("SELECT COUNT(*) c FROM send_log").fetchone()["c"],
        "total_pending": conn.execute("SELECT COUNT(*) c FROM post_queue WHERE status='pending_review'").fetchone()["c"],
        "total_drafts": conn.execute("SELECT COUNT(*) c FROM post_queue WHERE status='draft'").fetchone()["c"],
        "total_scheduled": conn.execute("SELECT COUNT(*) c FROM post_queue WHERE status='approved_scheduled'").fetchone()["c"],
        "total_rejected": conn.execute("SELECT COUNT(*) c FROM post_queue WHERE status='rejected'").fetchone()["c"],
    }
    conn.close()
    return nums

def notifications_for_role(role):
    conn = db()
    rows = conn.execute("""SELECT id,message,is_read,created_at FROM internal_notifications
                           WHERE instr(',' || target_roles || ',', ',' || ? || ',') > 0
                           ORDER BY id DESC LIMIT 30""", (role,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def search_history(query):
    q = f"%{clean_text(query)}%"
    conn = db()
    rows = conn.execute("""SELECT id,title,store,price_text,created_by,created_at,url FROM price_history
                           WHERE title LIKE ? OR store LIKE ? OR created_by LIKE ? OR url LIKE ?
                           ORDER BY id DESC LIMIT 50""", (q,q,q,q)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.route("/login", methods=["GET","POST"])
def login():
    if session.get("user_id"): return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        username = clean_text((request.form.get("username") or "").lower())
        password = request.form.get("password") or ""
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if user and user["is_active"] and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]; session["username"] = user["username"]; session["display_name"] = user["display_name"]; session["role"] = user["role"]
            return redirect(url_for("index"))
        error = "Usuário ou senha inválidos."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    return render_template("index.html",
        user_name=current_username(), user_role=current_role(),
        recent_posts=recent_posts(), team_users=team_users() if can_manage_users() else [],
        pending_posts=pending_posts() if can_review() else [], drafts=drafts(),
        approval_history=approval_history() if can_review() else [],
        dashboard_numbers=dashboard_numbers(), notifications=notifications_for_role(current_role())
    )

@app.route("/api/extract", methods=["POST"])
@login_required
def api_extract():
    url = clean_text((request.get_json(force=True) or {}).get("url") or "")
    if not url: return jsonify({"ok": False, "error": "Informe um link."}), 400
    if not url.startswith(("http://","https://")): url = "https://" + url
    try:
        return jsonify({"ok": True, "product": fetch_product(url)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Não consegui puxar automaticamente. Você pode preencher manualmente. Detalhe: {e}"}), 500

@app.route("/api/recalculate-heat", methods=["POST"])
@login_required
def api_recalc():
    product = enrich((request.get_json(force=True) or {}).get("product") or {}, preserve=True)
    return jsonify({"ok": True, "product": product})

@app.route("/api/save-history", methods=["POST"])
@login_required
def api_save_history():
    product = enrich((request.get_json(force=True) or {}).get("product") or {}, preserve=True)
    if not product.get("title") or not product.get("price"):
        return jsonify({"ok": False, "error": "Preencha título e preço antes de salvar."}), 400
    conn = db()
    conn.execute("""INSERT INTO price_history (product_key,title,store,url,price_value,price_text,image,coupon,created_by,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (product.get("product_key",""), product.get("title",""), product.get("store",""), product.get("url",""),
                  product.get("price_value"), product.get("price",""), product.get("image",""), product.get("coupon",""),
                  current_username(), now_str()))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "product": enrich(product, preserve=True)})

@app.route("/api/history-search")
@login_required
def api_history_search():
    return jsonify({"ok": True, "results": search_history(request.args.get("q",""))})

@app.route("/api/upload-image", methods=["POST"])
@login_required
def api_upload_image():
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Selecione uma imagem."}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".jpg",".jpeg",".png",".webp",".gif"):
        return jsonify({"ok": False, "error": "Formato não suportado."}), 400
    final_name = f"{uuid.uuid4().hex}{ext}"
    file.save(UPLOAD_DIR / final_name)
    return jsonify({"ok": True, "url": f"/static/uploads/{final_name}"})

@app.route("/api/save-draft", methods=["POST"])
@login_required
def api_save_draft():
    data = request.get_json(force=True) or {}
    product = enrich(data.get("product") or {}, preserve=True)
    caption = clean_multiline_text(data.get("caption") or "")
    image = clean_text(data.get("image") or product.get("image") or "")
    draft_id = data.get("draft_id")
    schedule_at = clean_text(data.get("schedule_at") or "")
    commission = clean_text(data.get("commission_estimate") or "") if is_admin() else ""
    conn = db()
    if draft_id:
        conn.execute("""UPDATE post_queue SET payload_json=?,caption=?,image=?,scheduled_for=?,commission_estimate=?,updated_at=?
                        WHERE id=? AND status='draft'""",
                     (json.dumps(product, ensure_ascii=False), caption, image, schedule_at, commission, now_str(), draft_id))
        conn.commit(); conn.close()
        return jsonify({"ok": True, "message": "Rascunho atualizado."})
    conn.execute("""INSERT INTO post_queue (payload_json,caption,image,created_by,status,scheduled_for,commission_estimate,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (json.dumps(product, ensure_ascii=False), caption, image, current_username(), "draft", schedule_at, commission, now_str(), now_str()))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "message": "Rascunho salvo."})

@app.route("/api/drafts/<int:draft_id>")
@login_required
def api_load_draft(draft_id):
    conn = db()
    row = conn.execute("SELECT * FROM post_queue WHERE id=? AND status='draft'", (draft_id,)).fetchone()
    conn.close()
    if not row: return jsonify({"ok": False, "error": "Rascunho não encontrado."}), 404
    return jsonify({"ok": True, "draft": {
        "id": row["id"], "caption": row["caption"], "image": row["image"], "scheduled_for": row["scheduled_for"],
        "commission_estimate": row["commission_estimate"], "product": json.loads(row["payload_json"])
    }})

@app.route("/api/send-telegram", methods=["POST"])
@login_required
def api_send():
    data = request.get_json(force=True) or {}
    caption = clean_multiline_text(data.get("caption") or "")
    image = clean_text(data.get("image") or "")
    product = enrich(data.get("product") or {}, preserve=True)
    schedule_at = clean_text(data.get("schedule_at") or "")
    commission = clean_text(data.get("commission_estimate") or "") if is_admin() else ""
    internal_comment = clean_text(data.get("internal_comment") or "")
    if not caption: return jsonify({"ok": False, "error": "Texto vazio."}), 400
    conn = db()
    role = current_role()
    if schedule_at:
        status = "pending_review" if role == "member" else "approved_scheduled"
        cur = conn.execute("""INSERT INTO post_queue (payload_json,caption,image,created_by,status,scheduled_for,commission_estimate,review_note,created_at,updated_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?)""",
                           (json.dumps(product, ensure_ascii=False), caption, image, current_username(), status, schedule_at, commission, internal_comment, now_str(), now_str()))
        post_id = cur.lastrowid
        conn.commit(); conn.close()
        add_approval(post_id, "created_scheduled", internal_comment)
        if role == "member":
            add_notification(f"Novo post de membro aguardando aprovação: {product.get('title','Sem título')}")
            return jsonify({"ok": True, "message": "Post agendado e enviado para aprovação."})
        return jsonify({"ok": True, "message": "Post agendado com sucesso."})
    if role == "member":
        cur = conn.execute("""INSERT INTO post_queue (payload_json,caption,image,created_by,status,commission_estimate,review_note,created_at,updated_at)
                              VALUES (?,?,?,?,?,?,?,?,?)""",
                           (json.dumps(product, ensure_ascii=False), caption, image, current_username(), "pending_review", commission, internal_comment, now_str(), now_str()))
        post_id = cur.lastrowid
        conn.commit(); conn.close()
        add_approval(post_id, "submitted_for_review", internal_comment)
        add_notification(f"Novo post de membro aguardando aprovação: {product.get('title','Sem título')}")
        return jsonify({"ok": True, "message": "Post enviado para aprovação do admin/subadmin."})
    conn.close()
    try:
        result = send_to_telegram(caption, image)
        conn = db()
        conn.execute("""INSERT INTO send_log (title,store,price_text,url,image,caption,sent_by,sent_at,source_status)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (product.get("title",""), product.get("store",""), product.get("price",""), product.get("url",""),
                      product.get("image",""), caption, current_username(), now_str(), "direct"))
        conn.commit(); conn.close()
        return jsonify({"ok": True, "message": "Enviado para o Telegram.", "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Falha ao enviar: {e}"}), 500

@app.route("/api/scheduled/<int:post_id>", methods=["PUT"])
@login_required
def api_edit_scheduled(post_id):
    if not can_review(): return jsonify({"ok": False, "error": "Sem permissão."}), 403
    data = request.get_json(force=True) or {}
    conn = db()
    row = conn.execute("SELECT * FROM post_queue WHERE id=? AND status='approved_scheduled'", (post_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"ok": False, "error": "Post agendado não encontrado."}), 404
    payload = data.get("product") or json.loads(row["payload_json"])
    caption = clean_multiline_text(data.get("caption") or row["caption"])
    image = clean_text(data.get("image") or row["image"] or "")
    scheduled_for = clean_text(data.get("scheduled_for") or row["scheduled_for"] or "")
    note = clean_text(data.get("note") or "")
    commission = clean_text(data.get("commission_estimate") or row["commission_estimate"] or "")
    conn.execute("""UPDATE post_queue SET payload_json=?,caption=?,image=?,scheduled_for=?,review_note=?,commission_estimate=?,updated_at=? WHERE id=?""",
                 (json.dumps(payload, ensure_ascii=False), caption, image, scheduled_for, note, commission if is_admin() else row["commission_estimate"], now_str(), post_id))
    conn.commit(); conn.close()
    add_approval(post_id, "edited_scheduled", note)
    return jsonify({"ok": True, "message": "Post agendado atualizado."})

@app.route("/api/pending-posts/<int:post_id>/approve", methods=["POST"])
@login_required
def api_approve(post_id):
    if not can_review(): return jsonify({"ok": False, "error": "Sem permissão."}), 403
    note = clean_text((request.get_json(force=True) or {}).get("note") or "")
    conn = db()
    row = conn.execute("SELECT * FROM post_queue WHERE id=?", (post_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"ok": False, "error": "Post não encontrado."}), 404
    payload = json.loads(row["payload_json"])
    if clean_text(row["scheduled_for"] or ""):
        conn.execute("UPDATE post_queue SET status='approved_scheduled',reviewer=?,review_note=?,updated_at=? WHERE id=?",
                     (current_username(), note, now_str(), post_id))
        conn.commit(); conn.close()
        add_approval(post_id, "approved_scheduled", note)
        return jsonify({"ok": True, "message": "Post aprovado e mantido no agendamento."})
    try:
        send_to_telegram(row["caption"], row["image"] or payload.get("image",""))
        conn.execute("UPDATE post_queue SET status='sent',reviewer=?,review_note=?,updated_at=? WHERE id=?",
                     (current_username(), note, now_str(), post_id))
        conn.execute("""INSERT INTO send_log (title,store,price_text,url,image,caption,sent_by,sent_at,source_status)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (payload.get("title",""), payload.get("store",""), payload.get("price",""), payload.get("url",""),
                      row["image"] or payload.get("image",""), row["caption"], row["created_by"], now_str(), "approved"))
        conn.commit(); conn.close()
        add_approval(post_id, "approved_and_sent", note)
        return jsonify({"ok": True, "message": "Post aprovado e enviado."})
    except Exception as e:
        conn.execute("UPDATE post_queue SET status='error',reviewer=?,review_note=?,updated_at=? WHERE id=?",
                     (current_username(), str(e)[:250], now_str(), post_id))
        conn.commit(); conn.close()
        return jsonify({"ok": False, "error": f"Erro ao enviar: {e}"}), 500

@app.route("/api/pending-posts/<int:post_id>/reject", methods=["POST"])
@login_required
def api_reject(post_id):
    if not can_review(): return jsonify({"ok": False, "error": "Sem permissão."}), 403
    note = clean_text((request.get_json(force=True) or {}).get("note") or "")
    conn = db()
    row = conn.execute("SELECT id FROM post_queue WHERE id=?", (post_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"ok": False, "error": "Post não encontrado."}), 404
    conn.execute("UPDATE post_queue SET status='rejected',reviewer=?,review_note=?,updated_at=? WHERE id=?",
                 (current_username(), note, now_str(), post_id))
    conn.commit(); conn.close()
    add_approval(post_id, "rejected", note)
    return jsonify({"ok": True, "message": "Post recusado."})

@app.route("/api/team-users", methods=["POST"])
@login_required
def api_create_user():
    if not can_manage_users(): return jsonify({"ok": False, "error": "Sem permissão."}), 403
    data = request.get_json(force=True) or {}
    username = clean_text((data.get("username") or "").lower())
    display_name = clean_text(data.get("display_name") or username)
    password = data.get("password") or ""
    role = clean_text(data.get("role") or "editor").lower()
    if role not in ("admin","subadmin","editor","member"): role = "editor"
    if current_role() == "subadmin" and role == "admin":
        return jsonify({"ok": False, "error": "Subadmin não pode criar administrador."}), 403
    if not username or not password:
        return jsonify({"ok": False, "error": "Preencha usuário e senha."}), 400
    conn = db()
    exists = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if exists:
        conn.close(); return jsonify({"ok": False, "error": "Esse usuário já existe."}), 400
    conn.execute("INSERT INTO users (username,display_name,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                 (username, display_name, generate_password_hash(password), role, now_str()))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "users": team_users()})

@app.route("/api/team-users/<int:user_id>", methods=["PUT"])
@login_required
def api_update_user(user_id):
    if not can_manage_users(): return jsonify({"ok": False, "error": "Sem permissão."}), 403
    data = request.get_json(force=True) or {}
    conn = db()
    target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not target:
        conn.close(); return jsonify({"ok": False, "error": "Usuário não encontrado."}), 404
    target = dict(target)
    new_role = clean_text(data.get("role") or target["role"]).lower()
    if new_role not in ("admin","subadmin","editor","member"): new_role = target["role"]
    if current_role() == "subadmin" and (target["role"] == "admin" or new_role == "admin"):
        conn.close(); return jsonify({"ok": False, "error": "Subadmin não pode alterar administrador."}), 403
    display_name = clean_text(data.get("display_name") or target["display_name"])
    is_active = 1 if str(data.get("is_active",1)) in ("1","true","True") else 0
    password = data.get("password") or ""
    if password:
        conn.execute("UPDATE users SET display_name=?, role=?, is_active=?, password_hash=? WHERE id=?",
                     (display_name, new_role, is_active, generate_password_hash(password), user_id))
    else:
        conn.execute("UPDATE users SET display_name=?, role=?, is_active=? WHERE id=?",
                     (display_name, new_role, is_active, user_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "users": team_users()})

@app.route("/api/team-users/<int:user_id>", methods=["DELETE"])
@login_required
def api_delete_user(user_id):
    if not can_manage_users(): return jsonify({"ok": False, "error": "Sem permissão."}), 403
    conn = db()
    target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not target:
        conn.close(); return jsonify({"ok": False, "error": "Usuário não encontrado."}), 404
    target = dict(target)
    if target["username"] == DEFAULT_ADMIN_USERNAME or target["role"] == "admin":
        if current_role() != "admin":
            conn.close(); return jsonify({"ok": False, "error": "Subadmin não pode excluir administrador."}), 403
        if target["username"] == session.get("username"):
            conn.close(); return jsonify({"ok": False, "error": "Você não pode excluir seu próprio admin."}), 400
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "users": team_users()})

@app.route("/api/notifications/<int:notification_id>/read", methods=["POST"])
@login_required
def api_read_notification(notification_id):
    conn = db()
    conn.execute("UPDATE internal_notifications SET is_read=1 WHERE id=?", (notification_id,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/dashboard-data")
@login_required
def api_dashboard():
    return jsonify({
        "ok": True,
        "recent_posts": recent_posts(),
        "team_users": team_users() if can_manage_users() else [],
        "pending_posts": pending_posts() if can_review() else [],
        "drafts": drafts(),
        "approval_history": approval_history() if can_review() else [],
        "dashboard_numbers": dashboard_numbers(),
        "notifications": notifications_for_role(current_role())
    })

@app.route("/health")
def health():
    return {"ok": True, "status": "running"}

init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
