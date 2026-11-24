"""Persistência síncrona executada em threads pelos serviços HTTP.

PostgreSQL no Compose; SQLite somente para demonstração local e testes.
O log de eventos é gravado na mesma transação das mensagens. O lock de escrita
ordena os commits do log para que leitores com cursor não pulem transações.
"""

from contextlib import contextmanager
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from .security import hash_password, token_hash, verify_password


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, url: str):
        self.url = url
        self.pg = url.startswith(("postgresql://", "postgres://"))
        if not self.pg:
            self.path = url.removeprefix("sqlite:///")
            Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self, write=False):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            db = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5)
        else:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                if self.pg:
                    db.execute("SELECT pg_advisory_xact_lock(482901)")
                else:
                    db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def sql(self, db, query, args=()):
        return db.execute(query.replace("?", "%s") if self.pg else query, args)

    def initialize(self, service):
        auth = [
            "CREATE TABLE IF NOT EXISTS auth_users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)",
            "CREATE TABLE IF NOT EXISTS auth_sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE, expires_at DOUBLE PRECISION NOT NULL)",
            "CREATE INDEX IF NOT EXISTS sessions_expiry ON auth_sessions(expires_at)",
        ]
        event_id = "BIGSERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        chat = [
            "CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, direct_key TEXT UNIQUE, owner_id TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)",
            "CREATE TABLE IF NOT EXISTS members (room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE, user_id TEXT NOT NULL, username TEXT NOT NULL, PRIMARY KEY(room_id,user_id))",
            "CREATE INDEX IF NOT EXISTS members_user ON members(user_id,room_id)",
            "CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id), sender_id TEXT NOT NULL, sender_name TEXT NOT NULL, content TEXT NOT NULL, client_id TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, UNIQUE(sender_id,client_id))",
            "CREATE INDEX IF NOT EXISTS messages_room_time ON messages(room_id,created_at,id)",
            f"CREATE TABLE IF NOT EXISTS events (id {event_id}, room_id TEXT NOT NULL REFERENCES rooms(id), payload TEXT NOT NULL)",
        ]
        with self.connection(write=True) as db:
            for query in auth if service == "auth" else chat:
                self.sql(db, query)
        if not self.pg:
            with self.connection() as db:
                db.execute("PRAGMA journal_mode=WAL")

    def healthy(self):
        with self.connection() as db:
            self.sql(db, "SELECT 1")
        return True

    def register(self, username, password):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,24}", username):
            raise DomainError("Use um usuário com 3 a 24 letras, números ou sublinhado.")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise DomainError("A senha deve conter entre 8 e 128 caracteres.")
        username = username.lower()
        encoded = hash_password(password)
        user = {"id": str(uuid.uuid4()), "username": username}
        with self.connection(write=True) as db:
            if self.sql(db, "SELECT id FROM auth_users WHERE username=?", (username,)).fetchone():
                raise DomainError("Este usuário já está cadastrado.", 409)
            self.sql(db, "INSERT INTO auth_users VALUES (?,?,?,?)", (user["id"], username, encoded, time.time()))
        return user

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 128:
            raise DomainError("Credenciais inválidas.", 401)
        with self.connection() as db:
            user = self.sql(db, "SELECT * FROM auth_users WHERE username=?", (username.lower(),)).fetchone()
        # Também executa scrypt quando o usuário não existe.
        encoded = user["password"] if user else hash_password("invalid-password", "00" * 16)
        valid = verify_password(password, encoded)
        if not user or not valid:
            raise DomainError("Credenciais inválidas.", 401)
        token = secrets.token_urlsafe(32)
        expires = time.time() + 8 * 3600
        with self.connection(write=True) as db:
            self.sql(db, "DELETE FROM auth_sessions WHERE expires_at < ?", (time.time(),))
            self.sql(db, "INSERT INTO auth_sessions VALUES (?,?,?)", (token_hash(token), user["id"], expires))
        return {"token": token, "expires_at": expires, "user": {"id": user["id"], "username": user["username"]}}

    def authenticate(self, token):
        with self.connection() as db:
            user = self.sql(db, "SELECT u.id,u.username FROM auth_users u JOIN auth_sessions s ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>?", (token_hash(token), time.time())).fetchone()
        if not user:
            raise DomainError("Sessão expirada. Entre novamente.", 401)
        return dict(user)

    def logout(self, token):
        with self.connection(write=True) as db:
            self.sql(db, "DELETE FROM auth_sessions WHERE token_hash=?", (token_hash(token),))

    def users(self):
        with self.connection() as db:
            return [dict(r) for r in self.sql(db, "SELECT id,username FROM auth_users ORDER BY username").fetchall()]

    def _member(self, db, room_id, user_id):
        if not self.sql(db, "SELECT 1 FROM members WHERE room_id=? AND user_id=?", (room_id, user_id)).fetchone():
            raise DomainError("Conversa indisponível para este usuário.", 403)

    def _room(self, db, room_id):
        room = dict(self.sql(db, "SELECT id,name,kind,owner_id,created_at FROM rooms WHERE id=?", (room_id,)).fetchone())
        room["members"] = [dict(r) for r in self.sql(db, "SELECT user_id AS id,username FROM members WHERE room_id=? ORDER BY username", (room_id,)).fetchall()]
        return room

    def create_room(self, actor, kind, name, members):
        people = {p["id"]: p for p in [actor, *members]}
        if kind not in ("direct", "group"):
            raise DomainError("Tipo de conversa inválido.")
        if kind == "direct" and len(people) != 2:
            raise DomainError("Uma conversa privada precisa de dois usuários.")
        if kind == "group" and not 2 <= len(people) <= 100:
            raise DomainError("Um grupo precisa de 2 a 100 participantes.")
        if not isinstance(name, str) or (kind == "group" and not 1 <= len(name.strip()) <= 60):
            raise DomainError("Informe um nome de grupo com até 60 caracteres.")
        direct_key = ":".join(sorted(people)) if kind == "direct" else None
        room_id = str(uuid.uuid4())
        with self.connection(write=True) as db:
            if direct_key:
                existing = self.sql(db, "SELECT id FROM rooms WHERE direct_key=?", (direct_key,)).fetchone()
                if existing:
                    return self._room(db, existing["id"])
            self.sql(db, "INSERT INTO rooms VALUES (?,?,?,?,?,?)", (room_id, name.strip() if kind == "group" else "", kind, direct_key, actor["id"], time.time()))
            for person in people.values():
                self.sql(db, "INSERT INTO members VALUES (?,?,?)", (room_id, person["id"], person["username"]))
            room = self._room(db, room_id)
            self.sql(db, "INSERT INTO events(room_id,payload) VALUES (?,?)", (room_id, json.dumps({"type": "room", "room": room})))
            return room

    def rooms(self, user_id):
        with self.connection() as db:
            ids = self.sql(db, "SELECT r.id FROM rooms r JOIN members m ON m.room_id=r.id WHERE m.user_id=? ORDER BY r.created_at DESC", (user_id,)).fetchall()
            return [self._room(db, row["id"]) for row in ids]

    def messages(self, room_id, user_id, before=None, limit=50):
        with self.connection() as db:
            self._member(db, room_id, user_id)
            args = [room_id]
            condition = ""
            if before:
                anchor = self.sql(db, "SELECT created_at,id FROM messages WHERE id=? AND room_id=?", (before, room_id)).fetchone()
                if not anchor:
                    raise DomainError("Cursor de histórico inválido.")
                condition = " AND (created_at < ? OR (created_at = ? AND id < ?))"
                args.extend([anchor["created_at"], anchor["created_at"], anchor["id"]])
            args.append(limit)
            rows = self.sql(db, "SELECT * FROM messages WHERE room_id=?" + condition + " ORDER BY created_at DESC,id DESC LIMIT ?", args).fetchall()
            return [dict(r) for r in reversed(rows)]

    def send(self, actor, room_id, content, client_id):
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 4000:
            raise DomainError("A mensagem deve conter entre 1 e 4000 caracteres.")
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", client_id):
            raise DomainError("Identificador de envio inválido.")
        with self.connection(write=True) as db:
            self._member(db, room_id, actor["id"])
            previous = self.sql(db, "SELECT * FROM messages WHERE sender_id=? AND client_id=?", (actor["id"], client_id)).fetchone()
            if previous:
                if previous["room_id"] != room_id or previous["content"] != content.strip():
                    raise DomainError("Identificador já usado em outra mensagem.", 409)
                return dict(previous)
            message = {"id": str(uuid.uuid4()), "room_id": room_id, "sender_id": actor["id"], "sender_name": actor["username"], "content": content.strip(), "client_id": client_id, "created_at": time.time()}
            self.sql(db, "INSERT INTO messages VALUES (?,?,?,?,?,?,?)", tuple(message.values()))
            self.sql(db, "INSERT INTO events(room_id,payload) VALUES (?,?)", (room_id, json.dumps({"type": "message", "message": message})))
            return message

    def event_head(self):
        with self.connection() as db:
            return self.sql(db, "SELECT COALESCE(MAX(id),0) AS head FROM events").fetchone()["head"]

    def events(self, after):
        with self.connection() as db:
            result = []
            for row in self.sql(db, "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 250", (after,)).fetchall():
                recipients = [r["user_id"] for r in self.sql(db, "SELECT user_id FROM members WHERE room_id=?", (row["room_id"],)).fetchall()]
                result.append((row["id"], json.loads(row["payload"]), recipients))
            return result
