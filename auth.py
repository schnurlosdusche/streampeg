import hashlib
from functools import wraps
from flask import request, Response
from db import get_setting


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_password(auth.password):
            return Response(
                "Login erforderlich",
                401,
                {"WWW-Authenticate": 'Basic realm="Streamripper UI"'},
            )
        return f(*args, **kwargs)
    return decorated


def _check_password(password):
    stored_hash = get_setting("auth_password_hash")
    if not stored_hash:
        return False
    return hashlib.sha256(password.encode()).hexdigest() == stored_hash
