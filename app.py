import sys
try:
    import flask  # noqa: F401
except ImportError:
    sys.exit("\nFlask is not installed for this Python:\n  %s\n\nFix: run this command, then start the app again:\n  \"%s\" -m pip install -r requirements.txt\n" % (sys.executable, sys.executable))
import os, secrets, socket, sqlite3, threading, webbrowser
from datetime import date, datetime, timezone
from functools import wraps
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE, "necteom.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))  # PostgreSQL if set, else local SQLite
if PG:
    import psycopg2, psycopg2.extras
    INTEGRITY = (psycopg2.IntegrityError,)
else:
    INTEGRITY = (sqlite3.IntegrityError,)


def load_secret():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(BASE, ".secret_key")
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        key = secrets.token_hex(32)
        try:
            with open(path, "w") as fh:
                fh.write(key)
        except OSError:
            pass
        return key


app = Flask(__name__)
app.secret_key = load_secret()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


class DB:
    """Tiny wrapper so the same SQL runs on SQLite (local) and PostgreSQL (hosting)."""
    def __init__(self):
        self.tx = False
        if PG:
            self.c = psycopg2.connect(DATABASE_URL, connect_timeout=15)
            self.c.autocommit = True
        else:
            self.c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
            self.c.row_factory = sqlite3.Row
            self.c.execute("PRAGMA foreign_keys=ON")

    def _cur(self, sql, args=()):
        if PG:
            cur = self.c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), args)
            return cur
        return self.c.execute(sql, args)

    def all(self, sql, args=()):
        cur = self._cur(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        if PG:
            cur.close()
        return rows

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def run(self, sql, args=()):
        cur = self._cur(sql, args)
        n = cur.rowcount
        if PG:
            cur.close()
        return n

    def insert(self, sql, args=()):
        if PG:
            return self.one(sql + " RETURNING id", args)["id"]
        return self._cur(sql, args).lastrowid

    def begin(self):  # exclusive write section: check + save cannot interleave between users
        self.run("BEGIN" if PG else "BEGIN IMMEDIATE")
        if PG:
            self.run("SELECT pg_advisory_xact_lock(7)")
        self.tx = True

    def commit(self):
        self.run("COMMIT")
        self.tx = False

    def rollback(self):
        if self.tx:
            self.run("ROLLBACK")
            self.tx = False

    def close(self):
        self.c.close()


PK = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY"
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users(id %s, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, password TEXT NOT NULL, role TEXT NOT NULL)" % PK,
    "CREATE TABLE IF NOT EXISTS rooms(id %s, name TEXT NOT NULL UNIQUE, capacity INTEGER)" % PK,
    "CREATE TABLE IF NOT EXISTS classes(id %s, trainer_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, UNIQUE(trainer_id, name))" % PK,
    "CREATE TABLE IF NOT EXISTS class_members(class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE, learner_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY(class_id, learner_id))",
    "CREATE TABLE IF NOT EXISTS bookings(id %s, room_id INTEGER NOT NULL REFERENCES rooms(id), trainer_id INTEGER NOT NULL REFERENCES users(id), class_id INTEGER REFERENCES classes(id) ON DELETE SET NULL, title TEXT NOT NULL, bdate TEXT NOT NULL, t_start TEXT NOT NULL, t_end TEXT NOT NULL)" % PK,
    "CREATE TABLE IF NOT EXISTS enrollments(booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE, learner_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY(booking_id, learner_id))",
    "CREATE TABLE IF NOT EXISTS notifications(id %s, user_id INTEGER NOT NULL REFERENCES users(id), message TEXT NOT NULL, created TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0)" % PK,
]


def init_db():
    c = DB()
    try:
        for stmt in SCHEMA:
            c.run(stmt)
        if not c.one("SELECT 1 AS x FROM rooms"):
            for name, cap in [("Room A", 12), ("Room B", 20), ("Room C", 8)]:
                c.run("INSERT INTO rooms(name, capacity) VALUES(?,?) ON CONFLICT DO NOTHING", (name, cap))
    finally:
        c.close()


init_db()


def db():
    if "db" not in g:
        g.db = DB()
    return g.db


@app.teardown_appcontext
def close_db(_):
    c = g.pop("db", None)
    if c is not None:
        c.close()


def to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


@app.template_filter("nice")
def nice(s):
    try:
        return date.fromisoformat(s).strftime("%a %d %b %Y")
    except (TypeError, ValueError):
        return s


@app.template_filter("initials")
def initials(name):
    return "".join(w[0] for w in (name or "?").split()[:2]).upper()


@app.before_request
def load_user():
    uid = session.get("uid")
    g.user = db().one("SELECT * FROM users WHERE id=?", (uid,)) if uid else None
    if request.method == "POST":
        token = session.get("_csrf")
        if not token or token != request.form.get("_csrf"):
            abort(400)


@app.context_processor
def inject():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return {"csrf": session["_csrf"], "user": g.get("user")}


def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            if not g.user:
                return redirect(url_for("login"))
            if role and g.user["role"] != role:
                abort(403)
            return fn(*a, **k)
        return wrapper
    return deco


def notify(c, user_id, message):
    c.run("INSERT INTO notifications(user_id, message, created) VALUES(?,?,?)",
          (user_id, message, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")))


def audience(c, bid, class_id):
    """Everyone who can see a booking: directly added learners + members of its class."""
    ids = {r["learner_id"] for r in c.all("SELECT learner_id FROM enrollments WHERE booking_id=?", (bid,))}
    if class_id:
        ids |= {r["learner_id"] for r in c.all("SELECT learner_id FROM class_members WHERE class_id=?", (class_id,))}
    return ids


def get_class(cid):
    row = db().one("SELECT * FROM classes WHERE id=? AND trainer_id=?", (cid, g.user["id"]))
    if not row:
        abort(404)
    return row


# ---------- accounts ----------
@app.route("/")
def home():
    if not g.user:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard" if g.user["role"] == "trainer" else "my_sessions"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        name, email = f.get("name", "").strip(), f.get("email", "").strip().lower()
        pw, role = f.get("password", ""), f.get("role", "")
        code = os.environ.get("TRAINER_CODE")
        err = None
        if not name or "@" not in email:
            err = "Please enter your name and a valid email."
        elif len(pw) < 8:
            err = "Password must be at least 8 characters."
        elif role not in ("trainer", "learner"):
            err = "Please choose a profile."
        elif role == "trainer" and code and f.get("code") != code:
            err = "Invalid trainer access code."
        if not err:
            try:
                db().run("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)",
                         (name, email, generate_password_hash(pw, method="pbkdf2:sha256"), role))
                flash("Account created. You can now log in.", "ok")
                return redirect(url_for("login"))
            except INTEGRITY:
                err = "This email is already registered."
        flash(err, "error")
    return render_template("auth.html", mode="register", need_code=bool(os.environ.get("TRAINER_CODE")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().one("SELECT * FROM users WHERE email=?", (request.form.get("email", "").strip().lower(),))
        if u and check_password_hash(u["password"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            return redirect(url_for("home"))
        flash("Incorrect email or password.", "error")
    return render_template("auth.html", mode="login")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- rooms & bookings (trainer) ----------
@app.route("/dashboard")
@login_required("trainer")
def dashboard():
    day = request.args.get("day", "")
    try:
        date.fromisoformat(day)
    except ValueError:
        day = date.today().isoformat()
    c = db()
    rooms = c.all("SELECT * FROM rooms ORDER BY name")
    rows = c.all("SELECT b.*, u.name AS trainer FROM bookings b JOIN users u ON u.id=b.trainer_id "
                 "WHERE b.bdate=? ORDER BY b.t_start", (day,))
    mins = lambda t: int(t[:2]) * 60 + int(t[3:5])
    for b in rows:  # position on the 07:00-21:00 timeline bar
        s, e = max(mins(b["t_start"]), 420), min(mins(b["t_end"]), 1260)
        b["left"], b["width"] = (s - 420) / 8.4, max((e - s) / 8.4, 1.5)
        b["show"] = e > s
    by_room = {r["id"]: [b for b in rows if b["room_id"] == r["id"]] for r in rooms}
    return render_template("dashboard.html", rooms=rooms, by_room=by_room, day=day)


def save_booking(existing, f, sel):
    """Validate and save a booking. Returns an error message, or None on success."""
    c = db()
    title = f.get("title", "").strip() or "Training session"
    room = c.one("SELECT * FROM rooms WHERE id=?", (to_int(f.get("room_id")) or 0,))
    if not room:
        return "Please choose a room."
    try:
        day = date.fromisoformat(f.get("bdate", ""))
    except ValueError:
        return "Please enter a valid date."
    if day < date.today():
        return "You cannot book a date in the past."
    start, end = f.get("t_start", "").strip(), f.get("t_end", "").strip()
    if not end:
        return "An end time is required."
    try:
        s, e = datetime.strptime(start, "%H:%M"), datetime.strptime(end, "%H:%M")
    except ValueError:
        return "Please enter valid start and end times."
    if e <= s:
        return "The end time must be after the start time."
    day, start, end = day.isoformat(), s.strftime("%H:%M"), e.strftime("%H:%M")
    class_id = to_int(f.get("class_id"))
    if f.get("class_id") and not c.one("SELECT 1 AS x FROM classes WHERE id=? AND trainer_id=?", (class_id or 0, g.user["id"])):
        return "Unknown class."
    c.begin()
    try:
        valid = {r["id"] for r in c.all("SELECT id FROM users WHERE role='learner'")}
        ids = {to_int(x) for x in sel} & valid
        bid = existing["id"] if existing else 0
        if c.one("SELECT 1 AS x FROM bookings WHERE room_id=? AND bdate=? AND t_start<? AND t_end>? AND id<>?",
                 (room["id"], day, end, start, bid)):
            c.rollback()
            return "This room is not available for that time slot (it overlaps another booking). Choose another time or room."
        label = "%s - %s, %s, %s-%s" % (title, room["name"], day, start, end)
        if existing:
            old = audience(c, bid, existing["class_id"])
            changed = (existing["room_id"], existing["bdate"], existing["t_start"], existing["t_end"], existing["title"]) != \
                      (room["id"], day, start, end, title)
            c.run("UPDATE bookings SET room_id=?, class_id=?, title=?, bdate=?, t_start=?, t_end=? WHERE id=?",
                  (room["id"], class_id, title, day, start, end, bid))
            c.run("DELETE FROM enrollments WHERE booking_id=?", (bid,))
        else:
            old, changed = set(), False
            bid = c.insert("INSERT INTO bookings(room_id,trainer_id,class_id,title,bdate,t_start,t_end) VALUES(?,?,?,?,?,?,?)",
                           (room["id"], g.user["id"], class_id, title, day, start, end))
        for i in ids:
            c.run("INSERT INTO enrollments(booking_id, learner_id) VALUES(?,?)", (bid, i))
        new = audience(c, bid, class_id)
        for i in new - old:
            notify(c, i, "New session: " + label)
        for i in (new & old) if changed else ():
            notify(c, i, "Session updated: " + label)
        for i in old - new:
            notify(c, i, "You were removed from the session: " + existing["title"])
        c.commit()
    except Exception:
        c.rollback()
        raise
    return None


def booking_form(bid=None):
    c = db()
    existing = None
    if bid:
        existing = c.one("SELECT * FROM bookings WHERE id=? AND trainer_id=?", (bid, g.user["id"]))
        if not existing:
            abort(404)
    sel = request.form.getlist("learners")
    if request.method == "POST":
        err = save_booking(existing, request.form, sel)
        if not err:
            flash("Booking saved.", "ok")
            return redirect(url_for("my_bookings"))
        flash(err, "error")
        data = request.form
    elif existing:
        data = dict(existing)
        sel = [str(r["learner_id"]) for r in c.all("SELECT learner_id FROM enrollments WHERE booking_id=?", (bid,))]
    else:
        data = {"room_id": request.args.get("room", ""), "bdate": request.args.get("day", date.today().isoformat()),
                "class_id": request.args.get("class", "")}
    rooms = c.all("SELECT * FROM rooms ORDER BY name")
    classes = c.all("SELECT id, name FROM classes WHERE trainer_id=? ORDER BY name", (g.user["id"],))
    learners = c.all("SELECT id, name, email FROM users WHERE role='learner' ORDER BY name")
    return render_template("book.html", rooms=rooms, classes=classes, learners=learners, data=data, sel=sel,
                           editing=bool(bid), today=date.today().isoformat())


@app.route("/book", methods=["GET", "POST"])
@login_required("trainer")
def book():
    return booking_form()


@app.route("/booking/<int:bid>/edit", methods=["GET", "POST"])
@login_required("trainer")
def edit(bid):
    return booking_form(bid)


@app.post("/booking/<int:bid>/cancel")
@login_required("trainer")
def cancel(bid):
    c = db()
    b = c.one("SELECT b.*, r.name AS room FROM bookings b JOIN rooms r ON r.id=b.room_id WHERE b.id=? AND b.trainer_id=?",
              (bid, g.user["id"]))
    if not b:
        abort(404)
    c.begin()
    try:
        for i in audience(c, bid, b["class_id"]):
            notify(c, i, "Session CANCELLED: %s - %s, %s, %s-%s" % (b["title"], b["room"], b["bdate"], b["t_start"], b["t_end"]))
        c.run("DELETE FROM bookings WHERE id=?", (bid,))
        c.commit()
    except Exception:
        c.rollback()
        raise
    flash("Booking cancelled. The room is free again and learners were notified.", "ok")
    return redirect(url_for("my_bookings"))


@app.route("/my-bookings")
@login_required("trainer")
def my_bookings():
    rows = db().all(
        "SELECT b.*, r.name AS room, c.name AS class_name, "
        "(SELECT COUNT(*) FROM (SELECT learner_id FROM enrollments WHERE booking_id=b.id UNION "
        "SELECT learner_id FROM class_members WHERE class_id=b.class_id) x) AS n "
        "FROM bookings b JOIN rooms r ON r.id=b.room_id LEFT JOIN classes c ON c.id=b.class_id "
        "WHERE b.trainer_id=? AND b.bdate>=? ORDER BY b.bdate, b.t_start", (g.user["id"], date.today().isoformat()))
    return render_template("my_bookings.html", bookings=rows)


# ---------- classes (trainer) ----------
@app.route("/classes", methods=["GET", "POST"])
@login_required("trainer")
def classes():
    c = db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Please enter a class name.", "error")
        else:
            try:
                cid = c.insert("INSERT INTO classes(trainer_id, name) VALUES(?,?)", (g.user["id"], name))
                flash("Class created. Now add your learners.", "ok")
                return redirect(url_for("class_detail", cid=cid))
            except INTEGRITY:
                flash("You already have a class with this name.", "error")
    rows = c.all("SELECT c.*, (SELECT COUNT(*) FROM class_members m WHERE m.class_id=c.id) AS n, "
                 "(SELECT COUNT(*) FROM bookings b WHERE b.class_id=c.id AND b.bdate>=?) AS sessions "
                 "FROM classes c WHERE c.trainer_id=? ORDER BY c.name", (date.today().isoformat(), g.user["id"]))
    return render_template("classes.html", classes=rows)


@app.route("/classes/<int:cid>")
@login_required("trainer")
def class_detail(cid):
    c, cls = db(), get_class(cid)
    members = c.all("SELECT u.id, u.name, u.email FROM class_members m JOIN users u ON u.id=m.learner_id "
                    "WHERE m.class_id=? ORDER BY u.name", (cid,))
    available = c.all("SELECT id, name, email FROM users WHERE role='learner' AND id NOT IN "
                      "(SELECT learner_id FROM class_members WHERE class_id=?) ORDER BY name", (cid,))
    sessions = c.all("SELECT b.*, r.name AS room FROM bookings b JOIN rooms r ON r.id=b.room_id "
                     "WHERE b.class_id=? AND b.bdate>=? ORDER BY b.bdate, b.t_start", (cid, date.today().isoformat()))
    return render_template("class_detail.html", cls=cls, members=members, available=available, sessions=sessions)


@app.post("/classes/<int:cid>/add")
@login_required("trainer")
def class_add(cid):
    c, cls = db(), get_class(cid)
    valid = {r["id"] for r in c.all("SELECT id FROM users WHERE role='learner'")}
    added = 0
    for x in request.form.getlist("learners"):
        lid = to_int(x)
        if lid in valid and c.run("INSERT INTO class_members(class_id, learner_id) VALUES(?,?) ON CONFLICT DO NOTHING", (cid, lid)):
            notify(c, lid, "You were added to the class '%s' (trainer: %s)." % (cls["name"], g.user["name"]))
            added += 1
    flash("%d learner(s) added." % added if added else "No learner selected.", "ok" if added else "error")
    return redirect(url_for("class_detail", cid=cid))


@app.post("/classes/<int:cid>/remove/<int:lid>")
@login_required("trainer")
def class_remove(cid, lid):
    c, cls = db(), get_class(cid)
    if c.run("DELETE FROM class_members WHERE class_id=? AND learner_id=?", (cid, lid)):
        notify(c, lid, "You were removed from the class '%s'." % cls["name"])
        flash("Learner removed from the class.", "ok")
    return redirect(url_for("class_detail", cid=cid))


@app.post("/classes/<int:cid>/delete")
@login_required("trainer")
def class_delete(cid):
    c, cls = db(), get_class(cid)
    for r in c.all("SELECT learner_id FROM class_members WHERE class_id=?", (cid,)):
        notify(c, r["learner_id"], "The class '%s' was deleted." % cls["name"])
    c.run("DELETE FROM classes WHERE id=?", (cid,))
    flash("Class deleted. Its existing bookings were kept without a class.", "ok")
    return redirect(url_for("classes"))


# ---------- learner ----------
@app.route("/my-sessions")
@login_required("learner")
def my_sessions():
    c, uid, today = db(), g.user["id"], date.today().isoformat()
    base = ("SELECT b.*, r.name AS room, u.name AS trainer, k.name AS class_name FROM bookings b "
            "JOIN rooms r ON r.id=b.room_id JOIN users u ON u.id=b.trainer_id LEFT JOIN classes k ON k.id=b.class_id "
            "WHERE (b.id IN (SELECT booking_id FROM enrollments WHERE learner_id=?) OR "
            "b.class_id IN (SELECT class_id FROM class_members WHERE learner_id=?)) AND ")
    upcoming = c.all(base + "b.bdate>=? ORDER BY b.bdate, b.t_start", (uid, uid, today))
    past = c.all(base + "b.bdate<? ORDER BY b.bdate DESC, b.t_start DESC LIMIT 10", (uid, uid, today))
    my_classes = c.all("SELECT k.name, u.name AS trainer, (SELECT COUNT(*) FROM class_members m2 WHERE m2.class_id=k.id) AS n "
                       "FROM class_members m JOIN classes k ON k.id=m.class_id JOIN users u ON u.id=k.trainer_id "
                       "WHERE m.learner_id=? ORDER BY k.name", (uid,))
    notes = c.all("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,))
    c.run("UPDATE notifications SET is_read=1 WHERE user_id=?", (uid,))
    return render_template("my_sessions.html", upcoming=upcoming, past=past, my_classes=my_classes, notes=notes)


def free_port(start):
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            try:
                sk.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


if __name__ == "__main__":
    port = free_port(int(os.environ.get("PORT", 5000)))
    url = "http://127.0.0.1:%d" % port
    print("\n  Necteom Room Booking is running.\n  Open: %s\n  Demo logins (password Demo1234): trainer1@demo.com / learner1@demo.com\n  Press Ctrl+C to stop.\n" % url)
    try:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
    app.run(host="127.0.0.1", port=port, debug=False)
