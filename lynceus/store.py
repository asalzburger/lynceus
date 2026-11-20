import contextlib
import json
import secrets
import sqlite3
import time
import uuid
from datetime import datetime,timezone
from argon2 import PasswordHasher
from . import config
from .contracts import canonical
from .policy import can_access

def uid():return str(uuid.uuid4())
def now():return datetime.now(timezone.utc).isoformat()
@contextlib.contextmanager
def db():
    c=sqlite3.connect(config.database_path(),timeout=30);c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:yield c;c.commit()
    except BaseException:c.rollback();raise
    finally:c.close()
def unpack(row):
    if row is None:return None
    r=dict(row)
    for key in ('record','bbox','payload','manifest','diagnostics','proposal','checks'):
        if key in r and r[key] is not None:r[key]=json.loads(r[key])
    return r
def initialize():
    config.prepare()
    with db() as c:
        c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE,password TEXT,role TEXT);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),csrf TEXT,expires REAL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,title TEXT,record TEXT,pdf_sha256 TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS plots(id TEXT PRIMARY KEY,source_id TEXT REFERENCES sources(id),page INTEGER,caption TEXT,bbox TEXT,scope TEXT DEFAULT 'pending',scope_note TEXT DEFAULT '',selected_attempt TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,plot_id TEXT REFERENCES plots(id),method TEXT,status TEXT,parent_id TEXT,created_at TEXT,manifest TEXT DEFAULT '{}',snapshot_sha256 TEXT,diagnostics TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,plot_id TEXT REFERENCES plots(id),source_id TEXT REFERENCES sources(id),kind TEXT,payload TEXT,state TEXT,worker TEXT,heartbeat REAL,created_at TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,plot_id TEXT REFERENCES plots(id),attempt_id TEXT REFERENCES attempts(id),snapshot_sha256 TEXT,decision TEXT,reviewer TEXT,reason TEXT,checks TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,plot_id TEXT REFERENCES plots(id),role TEXT,body TEXT,job_id TEXT,created_at TEXT);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state,kind,created_at);
CREATE INDEX IF NOT EXISTS attempts_plot ON attempts(plot_id);
''')
        for name,role in [('demo_owner','reviewer'),('demo_dev','builder')]:
            if not c.execute('SELECT 1 FROM users WHERE username=?',(name,)).fetchone():c.execute('INSERT INTO users VALUES(?,?,?,?)',(uid(),name,PasswordHasher().hash('daedalus'),role))
    token=config.DATA/'.agent-token'
    if not token.exists():token.write_text(secrets.token_urlsafe(40));token.chmod(0o600)
def user(row):
    r=dict(row);r.pop('password',None)
    r.update(groups=['group:collaboration-demo'],workspaces=['workspace:metis-demo'],agreements=[])
    return r
def source(sid,actor):
    with db() as c:r=unpack(c.execute('SELECT * FROM sources WHERE id=?',(sid,)).fetchone())
    if not r or not can_access(r['record']['protection'],actor):raise KeyError('Source not found')
    return r
def plot(pid,actor):
    with db() as c:r=unpack(c.execute('SELECT * FROM plots WHERE id=?',(pid,)).fetchone())
    if not r:raise KeyError('Plot not found')
    r['source']=source(r['source_id'],actor)
    return r
def attempt(aid,actor):
    with db() as c:r=unpack(c.execute('SELECT * FROM attempts WHERE id=?',(aid,)).fetchone())
    if not r:raise KeyError('Extraction not found')
    plot(r['plot_id'],actor)
    return r
def queue(kind,pid=None,sid=None,payload=None):
    jid=uid()
    with db() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,NULL,NULL,?,NULL)',(jid,pid,sid,kind,canonical(payload or {}),'queued',now()))
    return jid
def claim(worker,agent=False):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE jobs SET state='failed',error='Runner lease expired; retry this attempt' WHERE state='running' AND heartbeat<?",(time.time()-600,))
        operator='=' if agent else '!='
        row=c.execute(f"SELECT * FROM jobs WHERE state='queued' AND kind {operator} 'agent' ORDER BY created_at LIMIT 1").fetchone()
        if not row:return None
        c.execute("UPDATE jobs SET state='running',worker=?,heartbeat=? WHERE id=?",(worker,time.time(),row['id']))
        return unpack(row)
def finish(jid,error=None):
    with db() as c:c.execute('UPDATE jobs SET state=?,error=?,heartbeat=? WHERE id=?',('failed' if error else 'done',str(error)[:2000] if error else None,time.time(),jid))
