import os, re, json, sqlite3, secrets
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "instance"))
UPLOAD_DIR = DATA_DIR / "uploads"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.jinja_env.filters["from_json"] = json.loads
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "false").lower() == "true"
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".png", ".jpg", ".jpeg"}
DB_PATH = DATA_DIR / "igris.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          created_at TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'user'
        );
        CREATE TABLE IF NOT EXISTS documents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          original_name TEXT NOT NULL,
          stored_name TEXT NOT NULL,
          file_type TEXT NOT NULL,
          extracted_text TEXT NOT NULL DEFAULT '',
          uploaded_at TEXT NOT NULL,
          subject TEXT NOT NULL DEFAULT 'Unsorted'
        );
        CREATE TABLE IF NOT EXISTS notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          body TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          subject TEXT NOT NULL DEFAULT 'Personal'
        );
        CREATE TABLE IF NOT EXISTS quizzes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
          questions_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
          score INTEGER NOT NULL,
          total INTEGER NOT NULL,
          answers_json TEXT NOT NULL,
          attempted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subjects (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '#8b5cf6', created_at TEXT NOT NULL,
          UNIQUE(user_id, name)
        );
        CREATE TABLE IF NOT EXISTS flashcard_decks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          subject TEXT NOT NULL DEFAULT 'Unsorted', title TEXT NOT NULL, cards_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS study_tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title TEXT NOT NULL, subject TEXT NOT NULL DEFAULT 'Personal', due_date TEXT NOT NULL DEFAULT '',
          done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS study_sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          subject TEXT NOT NULL DEFAULT 'General', seconds INTEGER NOT NULL, studied_on TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rewards (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title TEXT NOT NULL, cost INTEGER NOT NULL, emoji TEXT NOT NULL DEFAULT '🎁', created_at TEXT NOT NULL,
          budget_rupees INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS redemptions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          reward_id INTEGER REFERENCES rewards(id) ON DELETE SET NULL, title TEXT NOT NULL, cost INTEGER NOT NULL,
          redeemed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS study_activity (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          activity_date TEXT NOT NULL, activity TEXT NOT NULL, UNIQUE(user_id, activity_date)
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        # Safe, idempotent migrations for databases created by the earlier ZIP.
        for table, column, definition in [
            ("users", "role", "TEXT NOT NULL DEFAULT 'user'"),
            ("documents", "subject", "TEXT NOT NULL DEFAULT 'Unsorted'"),
            ("notes", "subject", "TEXT NOT NULL DEFAULT 'Personal'"),
            ("rewards", "budget_rupees", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        if admin_email:
            con.execute("UPDATE users SET role='admin' WHERE lower(email)=?", (admin_email,))
init_db()

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with db() as con:
        return con.execute("SELECT id, username, email, role FROM users WHERE id=?", (uid,)).fetchone()

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in to continue.", "info")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def allowed_file(filename):
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS

def extract_text(path, ext):
    try:
        if ext in {".png", ".jpg", ".jpeg"}:
            from PIL import Image
            import pytesseract
            return pytesseract.image_to_string(Image.open(path)).strip()
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            chunks = [(page.extract_text() or "") for page in reader.pages]
            text = "\n\n".join(chunks).strip()
            if text:
                return text
            # OCR fallback for scanned/image-only PDFs. Requires system packages
            # poppler-utils and tesseract-ocr.
            try:
                from pdf2image import convert_from_path
                import pytesseract
                pages = convert_from_path(str(path), dpi=180)
                return "\n\n".join(pytesseract.image_to_string(page) for page in pages).strip()
            except Exception as ocr_exc:
                raise ValueError("This looks like a scanned PDF. Install tesseract-ocr and poppler-utils for OCR, then retry. Details: " + str(ocr_exc)[:180])
        if ext == ".docx":
            from docx import Document
            doc = Document(str(path))
            paras = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    paras.append(" | ".join(cell.text.strip() for cell in row.cells))
            return "\n".join(paras).strip()
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as exc:
        raise ValueError(f"Could not read this file: {exc}")

def ai_generate_quiz(source_text, count=8, difficulty="mixed", topic=""):
    provider = os.getenv("AI_PROVIDER", "openai").lower().strip()
    model = os.getenv("AI_MODEL", "").strip()
    count = max(3, min(int(count), 20))
    source_text = source_text[:26000]
    if not source_text.strip():
        raise ValueError("There is no readable text to build a test from. Try a text-based PDF, DOCX, TXT, or Markdown file.")
    system = (
      "You create accurate study assessments based ONLY on the supplied learning material. "
      "Do not invent unsupported facts. Return valid JSON only, no markdown, with shape: "
      '{"title":"short title","questions":[{"question":"...","options":["A","B","C","D"],"correct_index":0,"explanation":"brief explanation grounded in source"}]}. '
      "Each question must have exactly four distinct options and one unambiguous correct_index from 0 to 3. "
      "Do not include answers in the question text."
    )
    prompt = f"Create {count} multiple-choice questions. Difficulty: {difficulty}. Topic preference: {topic or 'use the main topics in the material'}. If the source is too short, create fewer high-quality questions rather than making things up.\n\nSOURCE MATERIAL:\n{source_text}"
    if provider == "openai":
        import requests
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("OPENAI_API_KEY is missing. Copy .env.example to .env and add your API key.")
        model = model or "gpt-4.1-mini"
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0.2, "response_format": {"type": "json_object"},
                  "messages": [{"role":"system","content":system},{"role":"user","content":prompt}]},
            timeout=120
        )
        if response.status_code >= 400:
            raise ValueError(f"OpenAI API error ({response.status_code}): {response.text[:500]}")
        raw = response.json()["choices"][0]["message"]["content"]
    elif provider in ("gemini", "google"):
        import requests
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("GEMINI_API_KEY is missing. Copy .env.example to .env and add your API key.")
        model = model or "gemini-2.5-flash"
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        response = requests.post(endpoint, json={
            "systemInstruction":{"parts":[{"text":system}]},
            "contents":[{"role":"user","parts":[{"text":prompt}]}],
            "generationConfig":{"temperature":0.2,"responseMimeType":"application/json"}
        }, timeout=120)
        if response.status_code >= 400:
            raise ValueError(f"Gemini API error ({response.status_code}): {response.text[:500]}")
        raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    elif provider in ("openrouter", "router"):
        import requests
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is missing. Add it to your .env file.")
        model = model or "openrouter/free"
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "http://localhost"),
                "X-OpenRouter-Title": os.getenv("OPENROUTER_APP_NAME", "IGRIS Study Hub"),
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            timeout=120,
        )
        if response.status_code >= 400:
            try:
                error_message = response.json().get("error", {}).get("message", response.text[:500])
            except ValueError:
                error_message = response.text[:500]
            raise ValueError(f"OpenRouter API error ({response.status_code}): {error_message}")
        try:
            raw = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("OpenRouter returned an unexpected response. Try another model.") from exc
    elif provider in ("cerebras", "cerebras_ai"):
        import requests
        api_key = os.getenv("CEREBRAS_API_KEY", "").strip()
        if not api_key:
            raise ValueError("CEREBRAS_API_KEY is missing. Add it to your .env file.")
        model = model or "gpt-oss-120b"
        response = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}], "temperature": 0.2},
            timeout=120,
        )
        if response.status_code >= 400:
            try:
                error_message = response.json().get("error", {}).get("message", response.text[:500])
            except ValueError:
                error_message = response.text[:500]
            raise ValueError(f"Cerebras API error ({response.status_code}): {error_message}")
        try:
            raw = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Cerebras returned an unexpected response. Check the model name and try again.") from exc
    elif provider in ("groq", "groqcloud"):
        import requests
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise ValueError("GROQ_API_KEY is missing. Add it to your .env file.")
        model = model or "openai/gpt-oss-120b"
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0.2,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": prompt}]},
            timeout=120,
        )
        if response.status_code >= 400:
            try:
                error_message = response.json().get("error", {}).get("message", response.text[:500])
            except ValueError:
                error_message = response.text[:500]
            raise ValueError(f"Groq API error ({response.status_code}): {error_message}")
        try:
            raw = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Groq returned an unexpected response. Check the model name and try again.") from exc
    else:
        raise ValueError("AI_PROVIDER must be 'openai', 'gemini', 'openrouter', 'cerebras', or 'groq'.")
    try:
        parsed = json.loads(raw)
        title = str(parsed.get("title") or topic or "Practice test")[:120]
        questions = parsed.get("questions", [])
        cleaned = []
        for q in questions[:count]:
            opts = q.get("options", [])
            idx = q.get("correct_index")
            if isinstance(q.get("question"), str) and len(opts) == 4 and isinstance(idx, int) and 0 <= idx <= 3:
                cleaned.append({"question": q["question"][:1000], "options":[str(o)[:500] for o in opts],
                                "correct_index":idx, "explanation":str(q.get("explanation",""))[:1500]})
        if not cleaned:
            raise ValueError("The model returned no valid questions. Try another document or model.")
        return title, cleaned
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("The AI response was not valid quiz JSON. Please retry.") from exc



def ai_json(system, prompt, fallback_model=None):
    """Call the configured server-side AI provider and parse a JSON response."""
    import requests
    provider = os.getenv("AI_PROVIDER", "openai").lower().strip()
    model = os.getenv("AI_MODEL", "").strip()
    if provider == "openai":
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key: raise ValueError("Add OPENAI_API_KEY to your .env file to use AI features.")
        model = model or (fallback_model or "gpt-4.1-mini")
        resp = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model":model,"temperature":0.2,"response_format":{"type":"json_object"},
                  "messages":[{"role":"system","content":system},{"role":"user","content":prompt}]}, timeout=120)
        if resp.status_code >= 400: raise ValueError(f"OpenAI API error ({resp.status_code}): {resp.text[:300]}")
        raw = resp.json()["choices"][0]["message"]["content"]
    elif provider in ("gemini", "google"):
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not key: raise ValueError("Add GEMINI_API_KEY to your .env file to use AI features.")
        model = model or (fallback_model or "gemini-2.5-flash")
        endpoint=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        resp=requests.post(endpoint,json={"systemInstruction":{"parts":[{"text":system}]},
            "contents":[{"role":"user","parts":[{"text":prompt}]}],
            "generationConfig":{"temperature":0.2,"responseMimeType":"application/json"}},timeout=120)
        if resp.status_code >= 400: raise ValueError(f"Gemini API error ({resp.status_code}): {resp.text[:300]}")
        raw=resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    elif provider in ("openrouter", "router"):
        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise ValueError("Add OPENROUTER_API_KEY to your .env file to use OpenRouter.")
        model = model or (fallback_model or "openrouter/free")
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "http://localhost"),
                "X-OpenRouter-Title": os.getenv("OPENROUTER_APP_NAME", "IGRIS Study Hub"),
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            timeout=120,
        )
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error", {}).get("message", resp.text[:400])
            except ValueError:
                message = resp.text[:400]
            raise ValueError(f"OpenRouter API error ({resp.status_code}): {message}")
        try:
            raw = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("OpenRouter returned an unexpected response. Try another model.") from exc
    elif provider in ("cerebras", "cerebras_ai"):
        import requests
        api_key = os.getenv("CEREBRAS_API_KEY", "").strip()
        if not api_key:
            raise ValueError("CEREBRAS_API_KEY is missing. Add it to your .env file.")
        model = model or "gpt-oss-120b"
        resp = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}], "temperature": 0.2},
            timeout=120,
        )
        if resp.status_code >= 400:
            try:
                error_message = resp.json().get("error", {}).get("message", resp.text[:500])
            except ValueError:
                error_message = resp.text[:500]
            raise ValueError(f"Cerebras API error ({response.status_code}): {error_message}")
        try:
            raw = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Cerebras returned an unexpected response. Check the model name and try again.") from exc
    elif provider in ("groq", "groqcloud"):
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise ValueError("Add GROQ_API_KEY to your .env file to use Groq AI features.")
        model = model or (fallback_model or "openai/gpt-oss-120b")
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0.2,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": prompt}]},
            timeout=120,
        )
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error", {}).get("message", resp.text[:400])
            except ValueError:
                message = resp.text[:400]
            raise ValueError(f"Groq API error ({resp.status_code}): {message}")
        try:
            raw = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Groq returned an unexpected response. Check the model name and try again.") from exc
    else: raise ValueError("AI_PROVIDER must be 'openai', 'gemini', 'openrouter', 'cerebras', or 'groq'.")
    try: return json.loads(raw)
    except json.JSONDecodeError as exc: raise ValueError("The AI model returned invalid JSON. Please retry.") from exc

def mark_activity(user_id, activity="study"):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO study_activity(user_id,activity_date,activity) VALUES(?,?,?)",
                    (user_id, datetime.utcnow().date().isoformat(), activity))

def study_coins(user_id):
    """One coin per completed study minute, capped at 120 coins per day."""
    with db() as con:
        rows = con.execute("SELECT studied_on,seconds FROM study_sessions WHERE user_id=?", (user_id,)).fetchall()
        redeemed = con.execute("SELECT COALESCE(SUM(cost),0) FROM redemptions WHERE user_id=?", (user_id,)).fetchone()[0]
    daily = {}
    for row in rows:
        daily[row["studied_on"][:10]] = daily.get(row["studied_on"][:10], 0) + row["seconds"]
    earned = sum(min(120, seconds // 60) for seconds in daily.values())
    return max(0, earned - redeemed)

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:;"
    return response

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("landing.html", user=None)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if len(username) < 2 or len(username) > 40:
            flash("Username must be 2–40 characters.", "error")
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address.", "error")
        elif len(password) < 10:
            flash("Use a password with at least 10 characters.", "error")
        else:
            try:
                with db() as con:
                    role = "admin" if email == os.getenv("ADMIN_EMAIL", "").strip().lower() and email else "user"
                    cur = con.execute("INSERT INTO users(username,email,password_hash,created_at,role) VALUES(?,?,?,?,?)",
                        (username, email, generate_password_hash(password), datetime.utcnow().isoformat(), role))
                    session.clear()
                    session["user_id"] = cur.lastrowid
                    session["csrf_token"] = secrets.token_urlsafe(32)
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                flash("That username or email is already registered.", "error")
    return render_template("auth.html", mode="register", user=None)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ident = request.form.get("identity", "").strip().lower()
        password = request.form.get("password", "")
        with db() as con:
            user = con.execute("SELECT * FROM users WHERE lower(email)=? OR lower(username)=?", (ident, ident)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Incorrect username/email or password.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="login", user=None)

@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    with db() as con:
        docs = con.execute("SELECT id, original_name, file_type, uploaded_at, length(extracted_text) AS chars FROM documents WHERE user_id=? ORDER BY uploaded_at DESC", (user["id"],)).fetchall()
        notes = con.execute("SELECT id,title,updated_at FROM notes WHERE user_id=? ORDER BY updated_at DESC LIMIT 5", (user["id"],)).fetchall()
        quizzes = con.execute("SELECT id,title,created_at FROM quizzes WHERE user_id=? ORDER BY created_at DESC LIMIT 5", (user["id"],)).fetchall()
        stats = {
            "documents": con.execute("SELECT count(*) FROM documents WHERE user_id=?", (user["id"],)).fetchone()[0],
            "notes": con.execute("SELECT count(*) FROM notes WHERE user_id=?", (user["id"],)).fetchone()[0],
            "quizzes": con.execute("SELECT count(*) FROM quizzes WHERE user_id=?", (user["id"],)).fetchone()[0],
            "attempts": con.execute("SELECT count(*) FROM attempts WHERE user_id=?", (user["id"],)).fetchone()[0],
        }
    return render_template("dashboard.html", user=user, docs=docs, notes=notes, quizzes=quizzes, stats=stats)

@app.route("/documents", methods=["GET", "POST"])
@login_required
def documents():
    user = current_user()
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a document to upload.", "error")
            return redirect(url_for("documents"))
        original = f.filename
        ext = Path(original).suffix.lower()
        subject = (request.form.get("subject", "Unsorted").strip() or "Unsorted")[:80]
        if ext not in ALLOWED_EXTENSIONS:
            flash("Supported formats: PDF, DOCX, TXT, Markdown, PNG, JPG, and JPEG. Scanned PDFs/images use OCR when dependencies are installed.", "error")
            return redirect(url_for("documents"))
        stored = f"{user['id']}_{secrets.token_hex(16)}{ext}"
        path = UPLOAD_DIR / stored
        f.save(path)
        try:
            text = extract_text(path, ext)
            if not text:
                raise ValueError("No selectable text was found. Scanned/image-only PDFs need OCR before they can be used.")
            with db() as con:
                con.execute("INSERT INTO documents(user_id,original_name,stored_name,file_type,extracted_text,uploaded_at,subject) VALUES(?,?,?,?,?,?,?)",
                    (user["id"], secure_filename(original) or "document", stored, ext[1:], text, datetime.utcnow().isoformat(), subject))
            mark_activity(user["id"], "upload")
            flash(f"Uploaded and processed {original}. Extracted {len(text):,} characters.", "success")
        except Exception as exc:
            path.unlink(missing_ok=True)
            flash(str(exc), "error")
        return redirect(url_for("documents"))
    with db() as con:
        docs = con.execute("SELECT id,original_name,file_type,uploaded_at,length(extracted_text) AS chars FROM documents WHERE user_id=? ORDER BY uploaded_at DESC", (user["id"],)).fetchall()
    return render_template("documents.html", user=user, docs=docs)

@app.get("/documents/<int:doc_id>/text")
@login_required
def document_text(doc_id):
    user = current_user()
    with db() as con:
        doc = con.execute("SELECT original_name,extracted_text FROM documents WHERE id=? AND user_id=?", (doc_id,user["id"])).fetchone()
    if not doc: abort(404)
    return render_template("document_text.html", user=user, doc=doc)

@app.post("/documents/<int:doc_id>/delete")
@login_required
def delete_document(doc_id):
    user = current_user()
    with db() as con:
        doc = con.execute("SELECT stored_name FROM documents WHERE id=? AND user_id=?", (doc_id,user["id"])).fetchone()
        if doc:
            (UPLOAD_DIR / doc["stored_name"]).unlink(missing_ok=True)
            con.execute("DELETE FROM documents WHERE id=? AND user_id=?", (doc_id,user["id"]))
    flash("Document deleted.", "success")
    return redirect(url_for("documents"))

@app.route("/notes", methods=["GET", "POST"])
@login_required
def notes():
    user = current_user()
    if request.method == "POST":
        title=request.form.get("title","").strip(); body=request.form.get("body","").strip()
        subject=(request.form.get("subject","Personal").strip() or "Personal")[:80]
        note_id=request.form.get("note_id","").strip()
        if not title:
            flash("Enter a note title.", "error")
        else:
            with db() as con:
                if note_id:
                    con.execute("UPDATE notes SET title=?,body=?,subject=?,updated_at=? WHERE id=? AND user_id=?",
                        (title,body,subject,datetime.utcnow().isoformat(),note_id,user["id"]))
                else:
                    con.execute("INSERT INTO notes(user_id,title,body,created_at,updated_at,subject) VALUES(?,?,?,?,?,?)",
                        (user["id"],title,body,datetime.utcnow().isoformat(),datetime.utcnow().isoformat(),subject))
            mark_activity(user["id"], "note")
            flash("Note saved.", "success")
        return redirect(url_for("notes"))
    with db() as con:
        all_notes=con.execute("SELECT id,title,body,created_at,updated_at,subject FROM notes WHERE user_id=? ORDER BY updated_at DESC",(user["id"],)).fetchall()
    selected=None
    if request.args.get("edit"):
        selected=next((n for n in all_notes if n["id"]==int(request.args["edit"])),None)
    return render_template("notes.html",user=user,notes=all_notes,selected=selected)

@app.post("/notes/<int:note_id>/delete")
@login_required
def delete_note(note_id):
    user=current_user()
    with db() as con: con.execute("DELETE FROM notes WHERE id=? AND user_id=?",(note_id,user["id"]))
    flash("Note deleted.", "success")
    return redirect(url_for("notes"))

@app.route("/quizzes", methods=["GET", "POST"])
@login_required
def quizzes():
    user=current_user()
    if request.method=="POST":
        doc_id=request.form.get("document_id","").strip()
        count=request.form.get("count","8")
        difficulty=request.form.get("difficulty","mixed")
        topic=request.form.get("topic","").strip()
        with db() as con:
            if doc_id=="all":
                row=con.execute("SELECT group_concat(extracted_text, '\n\n') AS txt FROM documents WHERE user_id=?",(user["id"],)).fetchone()
                source=row["txt"] or ""
                source_id=None
            else:
                try: doc_num=int(doc_id)
                except: doc_num=-1
                doc=con.execute("SELECT id,original_name,extracted_text FROM documents WHERE id=? AND user_id=?",(doc_num,user["id"])).fetchone()
                source=doc["extracted_text"] if doc else ""
                source_id=doc["id"] if doc else None
                if doc: topic=topic or doc["original_name"]
        try:
            title, questions=ai_generate_quiz(source,count,difficulty,topic)
            with db() as con:
                cur=con.execute("INSERT INTO quizzes(user_id,title,source_document_id,questions_json,created_at) VALUES(?,?,?,?,?)",
                    (user["id"],title,source_id,json.dumps(questions,ensure_ascii=False),datetime.utcnow().isoformat()))
                quiz_id=cur.lastrowid
            flash(f"Generated a {len(questions)}-question practice test.", "success")
            return redirect(url_for("take_quiz",quiz_id=quiz_id))
        except Exception as exc:
            flash(str(exc), "error")
            return redirect(url_for("quizzes"))
    with db() as con:
        docs=con.execute("SELECT id,original_name FROM documents WHERE user_id=? ORDER BY uploaded_at DESC",(user["id"],)).fetchall()
        all_quizzes=con.execute("""SELECT q.id,q.title,q.created_at,d.original_name AS source,
             (SELECT count(*) FROM attempts a WHERE a.quiz_id=q.id AND a.user_id=q.user_id) AS attempts
             FROM quizzes q LEFT JOIN documents d ON d.id=q.source_document_id
             WHERE q.user_id=? ORDER BY q.created_at DESC""",(user["id"],)).fetchall()
    return render_template("quizzes.html",user=user,docs=docs,quizzes=all_quizzes)

@app.route("/quizzes/<int:quiz_id>", methods=["GET", "POST"])
@login_required
def take_quiz(quiz_id):
    user=current_user()
    with db() as con:
        quiz=con.execute("SELECT * FROM quizzes WHERE id=? AND user_id=?",(quiz_id,user["id"])).fetchone()
    if not quiz: abort(404)
    questions=json.loads(quiz["questions_json"])
    result=None
    if request.method=="POST":
        answers=[]
        score=0
        for i,q in enumerate(questions):
            raw=request.form.get(f"q{i}","")
            try: choice=int(raw)
            except: choice=-1
            correct=choice==q["correct_index"]
            if correct: score+=1
            answers.append({"choice":choice,"correct":correct})
        with db() as con:
            con.execute("INSERT INTO attempts(user_id,quiz_id,score,total,answers_json,attempted_at) VALUES(?,?,?,?,?,?)",
                (user["id"],quiz_id,score,len(questions),json.dumps(answers),datetime.utcnow().isoformat()))
        result={"score":score,"total":len(questions),"answers":answers}
        mark_activity(user["id"], "quiz")
    return render_template("take_quiz.html",user=user,quiz=quiz,questions=questions,result=result)

@app.get("/history")
@login_required
def history():
    user=current_user()
    with db() as con:
        attempts=con.execute("""SELECT a.*,q.title FROM attempts a JOIN quizzes q ON q.id=a.quiz_id
           WHERE a.user_id=? ORDER BY a.attempted_at DESC LIMIT 100""",(user["id"],)).fetchall()
    return render_template("history.html",user=user,attempts=attempts)



@app.route("/subjects", methods=["GET", "POST"])
@login_required
def subjects():
    user=current_user()
    if request.method == "POST":
        name=request.form.get("name", "").strip()[:80]
        if not name: flash("Enter a subject name.", "error")
        else:
            try:
                with db() as con: con.execute("INSERT INTO subjects(user_id,name,created_at) VALUES(?,?,?)", (user["id"],name,datetime.utcnow().isoformat()))
                flash("Subject created.", "success")
            except sqlite3.IntegrityError: flash("That subject already exists.", "info")
        return redirect(url_for("subjects"))
    with db() as con:
        rows=con.execute("""SELECT s.*, (SELECT count(*) FROM documents d WHERE d.user_id=s.user_id AND d.subject=s.name) docs,
          (SELECT count(*) FROM notes n WHERE n.user_id=s.user_id AND n.subject=s.name) notes FROM subjects s WHERE s.user_id=? ORDER BY s.name""",(user["id"],)).fetchall()
    return render_template("subjects.html",user=user,subjects=rows)

@app.post("/subjects/<int:subject_id>/delete")
@login_required
def delete_subject(subject_id):
    user=current_user()
    with db() as con:
        row=con.execute("SELECT name FROM subjects WHERE id=? AND user_id=?",(subject_id,user["id"])).fetchone()
        if row:
            con.execute("UPDATE documents SET subject='Unsorted' WHERE user_id=? AND subject=?",(user["id"],row["name"]))
            con.execute("UPDATE notes SET subject='Personal' WHERE user_id=? AND subject=?",(user["id"],row["name"]))
            con.execute("DELETE FROM subjects WHERE id=? AND user_id=?",(subject_id,user["id"]))
    flash("Subject deleted. Its notes and documents were kept.","success")
    return redirect(url_for("subjects"))

@app.route("/vault")
@login_required
def vault():
    user=current_user(); q=request.args.get("q","").strip()[:120]
    like=f"%{q}%"
    with db() as con:
        docs=con.execute("SELECT id,original_name,subject,uploaded_at FROM documents WHERE user_id=? AND (original_name LIKE ? OR extracted_text LIKE ? OR subject LIKE ?) ORDER BY uploaded_at DESC",(user["id"],like,like,like)).fetchall()
        notes_rows=con.execute("SELECT id,title,body,subject,updated_at FROM notes WHERE user_id=? AND (title LIKE ? OR body LIKE ? OR subject LIKE ?) ORDER BY updated_at DESC",(user["id"],like,like,like)).fetchall()
    return render_template("vault.html",user=user,q=q,docs=docs,notes=notes_rows)

@app.route("/study-tools", methods=["GET","POST"])
@login_required
def study_tools():
    user=current_user()
    if request.method=="POST":
        action=request.form.get("action","")
        with db() as con:
            if action=="task":
                title=request.form.get("title","").strip()[:160]; subject=request.form.get("subject","Personal").strip()[:80]; due=request.form.get("due_date","")[:10]
                if title: con.execute("INSERT INTO study_tasks(user_id,title,subject,due_date,created_at) VALUES(?,?,?,?,?)",(user["id"],title,subject,due,datetime.utcnow().isoformat()))
            elif action=="session":
                try: seconds=max(0,min(int(request.form.get("seconds","0")),8*3600))
                except ValueError: seconds=0
                subject=request.form.get("subject","General").strip()[:80] or "General"
                if seconds >= 60:
                    con.execute("INSERT INTO study_sessions(user_id,subject,seconds,studied_on) VALUES(?,?,?,?)",(user["id"],subject,seconds,datetime.utcnow().date().isoformat()))
                    flash(f"Study session saved — earned up to {min(120,seconds//60)} Igris coins before daily cap.","success")
                else: flash("Study for at least one minute before saving a session.","info")
            elif action=="reward":
                title=request.form.get("reward_title","").strip()[:100]; emoji=request.form.get("emoji","🎁")[:8]
                try: cost=max(1,min(10000,int(request.form.get("cost","30"))))
                except ValueError: cost=30
                try: budget=max(0,min(100000,int(request.form.get("budget_rupees","0"))))
                except ValueError: budget=0
                if title: con.execute("INSERT INTO rewards(user_id,title,cost,emoji,created_at,budget_rupees) VALUES(?,?,?,?,?,?)",(user["id"],title,cost,emoji,datetime.utcnow().isoformat(),budget))
            elif action=="redeem":
                try: rid=int(request.form.get("reward_id","0"))
                except ValueError: rid=0
                reward=con.execute("SELECT * FROM rewards WHERE id=? AND user_id=?",(rid,user["id"])).fetchone()
                if reward and study_coins(user["id"]) >= reward["cost"]:
                    con.execute("INSERT INTO redemptions(user_id,reward_id,title,cost,redeemed_at) VALUES(?,?,?,?,?)",(user["id"],rid,reward["title"],reward["cost"],datetime.utcnow().isoformat()))
                    flash(f"Reward unlocked: {reward['emoji']} {reward['title']}! Enjoy it responsibly.","success")
                else: flash("Not enough Igris coins yet — keep going!","error")
        if action == "session" and seconds >= 60:
            mark_activity(user["id"], "timer")
        return redirect(url_for("study_tools"))
    with db() as con:
        tasks=con.execute("SELECT * FROM study_tasks WHERE user_id=? ORDER BY done,due_date, id DESC",(user["id"],)).fetchall()
        rewards=con.execute("SELECT * FROM rewards WHERE user_id=? ORDER BY cost",(user["id"],)).fetchall()
        redemptions=con.execute("SELECT * FROM redemptions WHERE user_id=? ORDER BY redeemed_at DESC LIMIT 10",(user["id"],)).fetchall()
        subject_rows=con.execute("SELECT name FROM subjects WHERE user_id=? ORDER BY name",(user["id"],)).fetchall()
    return render_template("study_tools.html",user=user,tasks=tasks,rewards=rewards,redemptions=redemptions,coins=study_coins(user["id"]),subjects=[r["name"] for r in subject_rows])

@app.post("/tasks/<int:task_id>/toggle")
@login_required
def toggle_task(task_id):
    user=current_user()
    with db() as con:
        task=con.execute("SELECT done FROM study_tasks WHERE id=? AND user_id=?",(task_id,user["id"])).fetchone()
        if task:
            con.execute("UPDATE study_tasks SET done=? WHERE id=? AND user_id=?",(0 if task["done"] else 1,task_id,user["id"]))
            completed = not task["done"]
        else: completed = False
    if completed: mark_activity(user["id"],"task")
    return redirect(url_for("study_tools"))

@app.post("/tasks/<int:task_id>/delete")
@login_required
def delete_task(task_id):
    user=current_user()
    with db() as con: con.execute("DELETE FROM study_tasks WHERE id=? AND user_id=?",(task_id,user["id"]))
    return redirect(url_for("study_tools"))

@app.route("/flashcards", methods=["GET","POST"])
@login_required
def flashcards():
    user=current_user()
    if request.method=="POST":
        try: doc_id=int(request.form.get("document_id","0")); count=max(4,min(30,int(request.form.get("count","10"))))
        except ValueError: doc_id=0; count=10
        with db() as con:
            doc=con.execute("SELECT * FROM documents WHERE id=? AND user_id=?",(doc_id,user["id"])).fetchone()
        if not doc: flash("Choose one of your uploaded documents first.","error"); return redirect(url_for("flashcards"))
        try:
            data=ai_json("Create flashcards only from supplied study material. Return JSON {title:string,cards:[{front:string,back:string}]}. No outside facts.",f"Create {count} concise revision flashcards from this source.\nSOURCE:\n{doc['extracted_text'][:22000]}")
            cards=[{"front":str(c.get("front",""))[:400],"back":str(c.get("back",""))[:400]} for c in data.get("cards",[]) if c.get("front") and c.get("back")][:count]
            if not cards: raise ValueError("The model returned no usable cards. Try another document.")
            with db() as con:
                con.execute("INSERT INTO flashcard_decks(user_id,subject,title,cards_json,created_at) VALUES(?,?,?,?,?)",(user["id"],doc["subject"],str(data.get("title") or doc["original_name"])[:120],json.dumps(cards),datetime.utcnow().isoformat()))
            mark_activity(user["id"],"flashcards"); flash(f"Created {len(cards)} flashcards from {doc['original_name']}.","success")
        except Exception as exc: flash(str(exc),"error")
        return redirect(url_for("flashcards"))
    with db() as con:
        docs=con.execute("SELECT id,original_name,subject FROM documents WHERE user_id=? ORDER BY uploaded_at DESC",(user["id"],)).fetchall()
        decks=con.execute("SELECT * FROM flashcard_decks WHERE user_id=? ORDER BY created_at DESC",(user["id"],)).fetchall()
    return render_template("flashcards.html",user=user,docs=docs,decks=decks)

@app.get("/flashcards/<int:deck_id>")
@login_required
def flashcard_deck(deck_id):
    user=current_user()
    with db() as con: deck=con.execute("SELECT * FROM flashcard_decks WHERE id=? AND user_id=?",(deck_id,user["id"])).fetchone()
    if not deck: abort(404)
    return render_template("flashcard_deck.html",user=user,deck=deck,cards=json.loads(deck["cards_json"]))

@app.route("/chat", methods=["GET","POST"])
@login_required
def assistant_chat():
    user=current_user()
    if request.method=="POST":
        question=request.form.get("message","").strip()[:3000]
        if not question: flash("Type a question first.","info"); return redirect(url_for("assistant_chat"))
        with db() as con:
            docs=con.execute("SELECT original_name,subject,extracted_text FROM documents WHERE user_id=? ORDER BY uploaded_at DESC LIMIT 30",(user["id"],)).fetchall()
        material="\n\n".join(f"DOCUMENT: {d['original_name']} | SUBJECT: {d['subject']}\n{d['extracted_text'][:9000]}" for d in docs)[:28000]
        if not material: flash("Upload your own notes or documents before chatting with the study assistant.","info"); return redirect(url_for("documents"))
        try:
            answer=ai_json("You are a helpful study tutor. Answer ONLY using the user's supplied documents. If the answer is absent, say it is not in their uploaded material. Return JSON {answer:string}.",f"STUDENT QUESTION: {question}\n\nUPLOADED MATERIAL:\n{material}").get("answer","")
            answer=str(answer)[:8000]
            with db() as con:
                con.execute("INSERT INTO chat_messages(user_id,role,content,created_at) VALUES(?,?,?,?)",(user["id"],"user",question,datetime.utcnow().isoformat()))
                con.execute("INSERT INTO chat_messages(user_id,role,content,created_at) VALUES(?,?,?,?)",(user["id"],"assistant",answer,datetime.utcnow().isoformat()))
            mark_activity(user["id"],"chat")
        except Exception as exc: flash(str(exc),"error")
        return redirect(url_for("assistant_chat"))
    with db() as con: messages=con.execute("SELECT role,content,created_at FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 40",(user["id"],)).fetchall()
    return render_template("chat.html",user=user,messages=list(reversed(messages)))

@app.get("/analytics")
@login_required
def analytics():
    user=current_user()
    with db() as con:
        attempts=con.execute("SELECT a.score,a.total,a.attempted_at,q.title FROM attempts a JOIN quizzes q ON q.id=a.quiz_id WHERE a.user_id=? ORDER BY a.attempted_at DESC LIMIT 20",(user["id"],)).fetchall()
        sessions=[dict(r) for r in con.execute("SELECT subject, SUM(seconds) seconds, COUNT(*) sessions FROM study_sessions WHERE user_id=? GROUP BY subject ORDER BY seconds DESC",(user["id"],)).fetchall()]
        activity=con.execute("SELECT activity_date FROM study_activity WHERE user_id=? ORDER BY activity_date DESC",(user["id"],)).fetchall()
        totals={"documents":con.execute("SELECT count(*) FROM documents WHERE user_id=?",(user["id"],)).fetchone()[0],"notes":con.execute("SELECT count(*) FROM notes WHERE user_id=?",(user["id"],)).fetchone()[0],"tests":con.execute("SELECT count(*) FROM quizzes WHERE user_id=?",(user["id"],)).fetchone()[0],"attempts":len(attempts)}
    max_seconds=max((r["seconds"] for r in sessions), default=1) or 1
    for row in sessions: row["percent"]=round(row["seconds"] / max_seconds * 100)
    days={r["activity_date"] for r in activity}; today=datetime.utcnow().date()
    streak=0; cursor=today
    if cursor.isoformat() not in days: cursor-=timedelta(days=1)
    while cursor.isoformat() in days: streak+=1; cursor-=timedelta(days=1)
    average=round(sum((a["score"]/a["total"]*100) for a in attempts if a["total"])/max(1,sum(1 for a in attempts if a["total"])),1) if attempts else 0
    return render_template("analytics.html",user=user,attempts=attempts,sessions=sessions,totals=totals,streak=streak,average=average,coins=study_coins(user["id"]))

@app.get("/export/<kind>")
@login_required
def export_data(kind):
    import csv, io
    user=current_user()
    output=io.StringIO(); writer=csv.writer(output)
    with db() as con:
        if kind=="notes":
            writer.writerow(["title","subject","body","created_at","updated_at"])
            rows=con.execute("SELECT title,subject,body,created_at,updated_at FROM notes WHERE user_id=? ORDER BY updated_at DESC",(user["id"],)).fetchall()
            for r in rows: writer.writerow(list(r))
            filename="igris-notes.csv"
        elif kind=="history":
            writer.writerow(["test","score","total","percent","attempted_at"])
            rows=con.execute("SELECT q.title,a.score,a.total,a.attempted_at FROM attempts a JOIN quizzes q ON q.id=a.quiz_id WHERE a.user_id=? ORDER BY a.attempted_at DESC",(user["id"],)).fetchall()
            for r in rows: writer.writerow([r["title"],r["score"],r["total"],round(100*r["score"]/max(1,r["total"]),1),r["attempted_at"]])
            filename="igris-test-history.csv"
        elif kind=="flashcards":
            writer.writerow(["deck","subject","front","back"])
            decks=con.execute("SELECT title,subject,cards_json FROM flashcard_decks WHERE user_id=?",(user["id"],)).fetchall()
            for deck in decks:
                for card in json.loads(deck["cards_json"]): writer.writerow([deck["title"],deck["subject"],card["front"],card["back"]])
            filename="igris-flashcards.csv"
        else: abort(404)
    from flask import Response
    return Response("\ufeff"+output.getvalue(),mimetype="text/csv; charset=utf-8",headers={"Content-Disposition":f"attachment; filename={filename}"})


@app.get("/export-pdf/<kind>")
@login_required
def export_pdf(kind):
    """Generate an owner-scoped PDF export for notes or test history."""
    from io import BytesIO
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    user=current_user(); output=BytesIO()
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name="IGTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, leading=25, textColor=colors.HexColor("#26334b"), alignment=TA_LEFT, spaceAfter=12))
    styles.add(ParagraphStyle(name="IGBody", parent=styles["BodyText"], fontSize=9, leading=13, spaceAfter=6, wordWrap="CJK"))
    styles.add(ParagraphStyle(name="IGSub", parent=styles["Heading2"], fontSize=12, leading=16, textColor=colors.HexColor("#334155"), spaceBefore=8, spaceAfter=5))
    # Register DejaVu when present so most accented and Indic characters export more gracefully.
    font_path="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if Path(font_path).exists():
        try:
            pdfmetrics.registerFont(TTFont("IGDejaVu",font_path))
            styles["IGBody"].fontName="IGDejaVu"
            styles["IGTitle"].fontName="IGDejaVu"
            styles["IGSub"].fontName="IGDejaVu"
            styles["Italic"].fontName="IGDejaVu"
        except Exception: pass
    font_name=styles["IGBody"].fontName
    supported=set(pdfmetrics.getFont(font_name).face.charToGlyph) if font_name=="IGDejaVu" else set(range(256))
    def esc(value):
        cleaned="".join(ch if ord(ch) in supported else "?" for ch in str(value or ""))
        return escape(cleaned)
    story=[Paragraph("IGRIS STUDY HUB",styles["IGTitle"]),Paragraph("Personal export for "+esc(user["username"]),styles["IGBody"]),Spacer(1,5*mm)]
    with db() as con:
        if kind=="notes":
            rows=con.execute("SELECT title,subject,body,updated_at FROM notes WHERE user_id=? ORDER BY updated_at DESC",(user["id"],)).fetchall()
            story.append(Paragraph("Notes Vault",styles["Heading1"]))
            if not rows: story.append(Paragraph("No notes saved yet.",styles["IGBody"]))
            for row in rows:
                story.append(KeepTogether([Paragraph(esc(row["title"]),styles["IGSub"]),Paragraph("Subject: "+esc(row["subject"])+" · Updated: "+esc(row["updated_at"][:10]),styles["Italic"]),Paragraph(esc(row["body"] or "").replace("\n","<br/>"),styles["IGBody"]),Spacer(1,3*mm)]))
            filename="igris-notes.pdf"
        elif kind=="history":
            rows=con.execute("SELECT q.title,a.score,a.total,a.attempted_at FROM attempts a JOIN quizzes q ON q.id=a.quiz_id WHERE a.user_id=? ORDER BY a.attempted_at DESC",(user["id"],)).fetchall()
            story.append(Paragraph("Practice Test History",styles["Heading1"]))
            data=[["Test","Score","Percent","Attempted"]]
            for row in rows: data.append([Paragraph(esc(row["title"]),styles["IGBody"]),f"{row['score']}/{row['total']}",f"{round(100*row['score']/max(1,row['total']))}%",row["attempted_at"][:10]])
            if len(data)==1: data.append(["No attempts yet.","—","—","—"])
            table=Table(data,colWidths=[82*mm,25*mm,25*mm,38*mm],repeatRows=1)
            table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#dbeafe")),("TEXTCOLOR",(0,0),(-1,0),colors.HexColor("#1e293b")),("GRID",(0,0),(-1,-1),0.4,colors.HexColor("#cbd5e1")),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
            story.append(table); filename="igris-test-history.pdf"
        else: abort(404)
    SimpleDocTemplate(output,pagesize=A4,rightMargin=18*mm,leftMargin=18*mm,topMargin=18*mm,bottomMargin=18*mm,title="IGRIS Study Hub Export").build(story)
    from flask import send_file
    output.seek(0)
    return send_file(output,mimetype="application/pdf",as_attachment=True,download_name=filename)

@app.get("/admin")
@login_required
def admin_dashboard():
    user=current_user()
    if user["role"]!="admin": abort(403)
    with db() as con:
        users=con.execute("SELECT id,username,email,role,created_at FROM users ORDER BY created_at DESC").fetchall()
        stats={"users":len(users),"documents":con.execute("SELECT count(*) FROM documents").fetchone()[0],"quizzes":con.execute("SELECT count(*) FROM quizzes").fetchone()[0],"attempts":con.execute("SELECT count(*) FROM attempts").fetchone()[0]}
    return render_template("admin.html",user=user,users=users,stats=stats)

@app.post("/admin/users/<int:user_id>/role")
@login_required
def admin_role(user_id):
    user=current_user()
    if user["role"]!="admin": abort(403)
    role=request.form.get("role","user")
    if role not in {"user","admin"}: abort(400)
    if user_id==user["id"] and role!="admin": flash("You cannot remove your own admin role here.","error")
    else:
        with db() as con: con.execute("UPDATE users SET role=? WHERE id=?",(role,user_id))
        flash("User role updated.","success")
    return redirect(url_for("admin_dashboard"))


@app.get("/health")
def health():
    return jsonify({"status":"ok"})

if __name__ == "__main__":
    app.run(host=os.getenv("HOST","127.0.0.1"), port=int(os.getenv("PORT","5000")), debug=os.getenv("FLASK_DEBUG","false").lower()=="true")
