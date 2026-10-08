"""Persistent access decisions and Telegram file IDs; no download queue."""
import json
import secrets
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY, name TEXT, username TEXT,
                status TEXT NOT NULL, nonce TEXT NOT NULL, payload TEXT,
                notified INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        self.db.commit()

    def get(self, uid):
        return self.db.execute('SELECT * FROM users WHERE uid=?', (uid,)).fetchone()

    def request(self, user, payload=None):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO users(uid,name,username,status,nonce,payload) VALUES(?,?,?,?,?,?)',
                (user.id, user.full_name, user.username or '', 'pending', secrets.token_hex(8), payload))
            if payload:
                self.db.execute("UPDATE users SET payload=? WHERE uid=? AND status='pending' AND payload IS NULL", (payload, user.id))
        return self.get(user.id)

    def mark_notified(self, uid):
        with self.db:
            self.db.execute('UPDATE users SET notified=1 WHERE uid=?', (uid,))

    def decide(self, uid, nonce, status):
        if status not in ('approved', 'denied'):
            raise ValueError('Invalid decision')
        with self.db:
            row = self.get(uid)
            if not row or row['status'] != 'pending' or row['nonce'] != nonce:
                return None
            self.db.execute('UPDATE users SET status=?, payload=NULL WHERE uid=?', (status, uid))
            return dict(row)

    def set_status(self, uid, status):
        if status not in ('approved', 'denied'):
            raise ValueError('Invalid status')
        with self.db:
            self.db.execute('''INSERT INTO users(uid,name,username,status,nonce) VALUES(?,?,?,?,?)
                ON CONFLICT(uid) DO UPDATE SET status=excluded.status, nonce=excluded.nonce, payload=NULL''',
                (uid, '', '', status, secrets.token_hex(8)))

    def pending(self):
        return self.db.execute("SELECT * FROM users WHERE status='pending'").fetchall()

    def cached(self, key):
        row = self.db.execute('SELECT value FROM cache WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, key, files):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)', (key, json.dumps(files)))

    def uncache(self, key):
        with self.db:
            self.db.execute('DELETE FROM cache WHERE key=?', (key,))

    def close(self):
        self.db.close()
