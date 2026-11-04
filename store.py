"""Small durable SQLite ledger. Secrets live separately with owner-only permissions."""
import json
import os
import sqlite3
import time
from pathlib import Path


def atomic_json(path, value, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    if private:
        path.chmod(0o600)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'ledger.sqlite3'
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, key TEXT, data TEXT, updated REAL, PRIMARY KEY(kind,key))')
        for row in self.list('job'):
            if row['status'] in ('pending', 'running'):
                row.update(status='interrupted', message='进程重载中断；请检查已完成记录后重试')
                self.put('job', row['id'], row)

    def connect(self):
        return sqlite3.connect(self.db, timeout=20)

    def put(self, kind, key, data):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)', (kind, str(key), json.dumps(data, ensure_ascii=False), time.time()))

    def get(self, kind, key, default=None):
        with self.connect() as c:
            row = c.execute('SELECT data FROM records WHERE kind=? AND key=?', (kind, str(key))).fetchone()
        return json.loads(row[0]) if row else default

    def list(self, kind):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT data FROM records WHERE kind=? ORDER BY updated DESC', (kind,))]
