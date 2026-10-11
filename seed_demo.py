"""Optional: creates demo accounts + one demo class (password for all: Demo1234). Run: python seed_demo.py"""
from werkzeug.security import generate_password_hash
from app import app, db

USERS = [("Alice Trainer", "trainer1@demo.com", "trainer"), ("Bob Trainer", "trainer2@demo.com", "trainer"),
         ("Lea Learner", "learner1@demo.com", "learner"), ("Paul Learner", "learner2@demo.com", "learner"),
         ("Sara Learner", "learner3@demo.com", "learner")]
with app.app_context():
    c = db()
    for name, email, role in USERS:
        c.run("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
              (name, email, generate_password_hash("Demo1234", method="pbkdf2:sha256"), role))
    t = c.one("SELECT id FROM users WHERE email='trainer1@demo.com'")["id"]
    c.run("INSERT INTO classes(trainer_id, name) VALUES(?,?) ON CONFLICT DO NOTHING", (t, "Python Beginners"))
    cid = c.one("SELECT id FROM classes WHERE trainer_id=? AND name='Python Beginners'", (t,))["id"]
    for e in ("learner1@demo.com", "learner2@demo.com"):
        lid = c.one("SELECT id FROM users WHERE email=?", (e,))["id"]
        c.run("INSERT INTO class_members(class_id, learner_id) VALUES(?,?) ON CONFLICT DO NOTHING", (cid, lid))
print("Demo ready (password: Demo1234):", ", ".join(u[1] for u in USERS))
