import hashlib
import hmac
import re
import secrets
import time
import uuid

from sqlalchemy import select, insert, delete, update

from app.web.store import users, sessions, WebError, digest

ITERATIONS = 600_000


def password_hash(password):
    if not 12 <= len(password) <= 256:
        raise ValueError('A senha deve ter de 12 a 256 caracteres')
    salt = secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f'pbkdf2_sha256${ITERATIONS}${salt}${value}'


def password_matches(password, encoded):
    _, rounds, salt, value = encoded.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return hmac.compare_digest(value, actual)


def create_user(store, username, password, organization='dazumdigital'):
    username = username.strip().lower()
    if not re.fullmatch(r'[a-z0-9@._-]{3,120}', username):
        raise ValueError('Nome de usuário inválido')
    encoded = password_hash(password)
    with store.transaction() as conn:
        store.lock_org(conn, organization)
        conn.execute(insert(users).values(id=str(uuid.uuid4()), username=username, password_hash=encoded, organization_id=organization, active=1))


def login(store, username, password, hours):
    with store.engine.connect() as conn:
        user = conn.execute(select(users).where(users.c.username == username.strip().lower(), users.c.active == 1)).mappings().first()
    # Fixed work even when the account doesn't exist; no account enumeration.
    dummy = 'pbkdf2_sha256$600000$' + '00'*16 + '$' + '00'*32
    if not password_matches(password, user['password_hash'] if user else dummy) or not user:
        raise WebError('CREDENCIAIS_INVALIDAS', 401)
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with store.transaction() as conn:
        conn.execute(delete(sessions).where(sessions.c.expires_at <= time.time()))
        conn.execute(insert(sessions).values(token_hash=digest(token), user_id=user['id'], csrf=csrf, expires_at=time.time()+hours*3600))
    return token, csrf


def revoke(store, token):
    with store.transaction() as conn:
        conn.execute(delete(sessions).where(sessions.c.token_hash == digest(token)))
