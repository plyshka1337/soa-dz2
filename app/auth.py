import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import connect
from app.errors import ApiError

SECRET = os.environ["JWT_SECRET"]
bearer = HTTPBearer(auto_error=False)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + "$" + digest.hex()


def password_matches(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split("$")[0]), stored)


def token_pair(conn, user):
    now = datetime.now(timezone.utc)
    common = {"sub": str(user["id"]), "role": user["role"], "iat": now}
    access = jwt.encode({**common, "type": "access", "exp": now + timedelta(minutes=15)}, SECRET, algorithm="HS256")
    expires = now + timedelta(days=7)
    refresh = jwt.encode({**common, "type": "refresh", "jti": str(uuid4()), "exp": expires}, SECRET, algorithm="HS256")
    conn.execute("INSERT INTO refresh_tokens (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                 (hashlib.sha256(refresh.encode()).hexdigest(), user["id"], expires))
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}


def decode_token(token, expected):
    try:
        data = jwt.decode(token, SECRET, algorithms=["HS256"], options={"require": ["exp", "sub", "type", "role"]})
        if data["type"] != expected:
            raise jwt.InvalidTokenError()
        return data
    except jwt.ExpiredSignatureError:
        code = "TOKEN_EXPIRED" if expected == "access" else "REFRESH_TOKEN_INVALID"
        raise ApiError(401, code, "Token has expired") from None
    except jwt.InvalidTokenError:
        code = "TOKEN_INVALID" if expected == "access" else "REFRESH_TOKEN_INVALID"
        raise ApiError(401, code, "Invalid token") from None


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None:
        raise ApiError(401, "TOKEN_INVALID", "Bearer access token is required")
    data = decode_token(credentials.credentials, "access")
    with connect() as conn:
        user = conn.execute("SELECT id, username, role FROM users WHERE id::text = %s", (data["sub"],)).fetchone()
    if user is None or user["role"] != data["role"]:
        raise ApiError(401, "TOKEN_INVALID", "Unknown user or role")
    request.state.user_id = str(user["id"])
    return user


def require_roles(*roles):
    def check(user=Depends(current_user)):
        if user["role"] not in roles:
            raise ApiError(403, "ACCESS_DENIED", "Role cannot perform this operation")
        return user
    return check
