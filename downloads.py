"""Expiring, unguessable bearer links. Stream files and support HTTP ranges."""
import asyncio
import mimetypes
import os
import re
import secrets
import shutil
import time
from pathlib import Path
import tornado.web
import tornado.httpserver
import tornado.iostream


class Downloads:
    def __init__(self, store, allowed, base_url, root, ttl=3600):
        self.store, self.allowed = store, allowed
        self.base_url = base_url.rstrip('/')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.active = {}
        self.store.db.execute('CREATE TABLE IF NOT EXISTS links(token TEXT PRIMARY KEY, uid INTEGER, name TEXT, expires REAL)')
        self.store.db.commit()
        # Single-instance service: leftover jobs from an interrupted old process.
        for path in self.root.glob('work-*'):
            if path.is_dir(): shutil.rmtree(path)
        known = {r[0] for r in self.store.db.execute('SELECT name FROM links')}
        for path in self.root.iterdir():
            if path.is_file() and path.name not in known: path.unlink()
        self.cleanup()

    def publish(self, file, uid):
        if not self.allowed(uid): raise RuntimeError('ليس لديك إذن تحميل.')
        token = secrets.token_urlsafe(32)
        name = token + file.suffix.lower()
        target = self.root / name
        os.replace(file, target)  # Job folders and published files share one filesystem.
        try:
            with self.store.db:
                self.store.db.execute('INSERT INTO links VALUES(?,?,?,?)', (token, uid, name, time.time()+self.ttl))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return self.base_url + '/d/' + token

    def lookup(self, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token): return None
        row = self.store.db.execute('SELECT * FROM links WHERE token=?', (token,)).fetchone()
        if not row or row['expires'] <= time.time() or not self.allowed(row['uid']): return None
        path = self.root / row['name']
        return (path, row['uid']) if path.is_file() else None

    def cleanup(self):
        for row in self.store.db.execute('SELECT * FROM links').fetchall():
            if self.active.get(row['token']): continue
            if row['expires'] <= time.time() or not self.allowed(row['uid']):
                (self.root / row['name']).unlink(missing_ok=True)
                with self.store.db:
                    self.store.db.execute('DELETE FROM links WHERE token=?', (row['token'],))

    def application(self):
        return tornado.web.Application([(r'/healthz', Health), (r'/d/([A-Za-z0-9_-]+)', Download, {'manager': self})],
                                       log_function=lambda handler: None)


class Health(tornado.web.RequestHandler):
    def get(self): self.write({'status': 'ok'})


def byte_range(header, size):
    if not header: return 0, size-1, False
    m = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not m or not any(m.groups()): raise ValueError('Invalid range')
    first, last = m.groups()
    if first:
        start = int(first)
        end = min(int(last), size-1) if last else size-1
    else:
        suffix = int(last)
        if suffix <= 0: raise ValueError('Invalid range')
        start, end = max(0, size-suffix), size-1
    if start >= size or end < start: raise ValueError('Unsatisfiable range')
    return start, end, True


class Download(tornado.web.RequestHandler):
    def initialize(self, manager): self.manager = manager
    async def head(self, token): await self.get(token, head=True)
    async def get(self, token, head=False):
        info = self.manager.lookup(token)
        if not info: raise tornado.web.HTTPError(404)
        path, uid = info
        size = path.stat().st_size
        try:
            start, end, partial = byte_range(self.request.headers.get('Range'), size)
        except ValueError:
            self.set_header('Content-Range', f'bytes */{size}')
            raise tornado.web.HTTPError(416)
        self.set_header('Cache-Control', 'private, no-store')
        self.set_header('Referrer-Policy', 'no-referrer')
        self.set_header('X-Content-Type-Options', 'nosniff')
        self.set_header('Accept-Ranges', 'bytes')
        self.set_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.set_header('Content-Disposition', f'attachment; filename="download{path.suffix}"')
        self.set_header('Content-Length', str(end-start+1))
        if partial:
            self.set_status(206)
            self.set_header('Content-Range', f'bytes {start}-{end}/{size}')
        if head: return
        self.manager.active[token] = self.manager.active.get(token, 0)+1
        try:
            with path.open('rb') as file:
                file.seek(start)
                remaining = end-start+1
                while remaining:
                    if not self.manager.allowed(uid):
                        self.request.connection.close()
                        return
                    chunk = await asyncio.to_thread(file.read, min(256*1024, remaining))
                    if not chunk: break
                    remaining -= len(chunk)
                    self.write(chunk)
                    await self.flush()
        except tornado.iostream.StreamClosedError:
            pass
        finally:
            self.manager.active[token] -= 1
            if not self.manager.active[token]: self.manager.active.pop(token)
