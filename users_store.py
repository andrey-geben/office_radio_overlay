# users_store.py
import os
import json
import threading
import time
from werkzeug.security import generate_password_hash, check_password_hash

import sys
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "assets", "users.json")
_lock = threading.Lock()


def _ensure():
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)


def _read():
    _ensure()
    if not os.path.exists(USERS_FILE):
        return []
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write(data):
    _ensure()
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def count():
    return len(_read())


def list_users():
    """Возвращает список без хэшей."""
    return [
        {k: v for k, v in u.items() if k != "password_hash"}
        for u in _read()
    ]


def get_user(username):
    for u in _read():
        if u["username"] == username:
            return u
    return None


def verify(username, password):
    user = get_user(username)
    if not user:
        return None
    try:
        if check_password_hash(user["password_hash"], password):
            return user
    except Exception:
        return None
    return None


def create_user(username, password, role="operator", must_change=True):
    if role not in ("admin", "operator"):
        raise ValueError("role должен быть 'admin' или 'operator'")
    username = (username or "").strip()
    if not username:
        raise ValueError("Логин не может быть пустым")
    if len(username) < 3:
        raise ValueError("Логин должен быть минимум 3 символа")
    if not password or len(password) < 4:
        raise ValueError("Пароль должен быть минимум 4 символа")

    with _lock:
        data = _read()
        if any(u["username"] == username for u in data):
            raise ValueError(f"Пользователь '{username}' уже существует")
        data.append({
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": role,
            "must_change_password": bool(must_change),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        _write(data)
    return get_user(username)


def update_password(username, new_password, must_change=False):
    if not new_password or len(new_password) < 4:
        raise ValueError("Пароль должен быть минимум 4 символа")
    with _lock:
        data = _read()
        found = False
        for u in data:
            if u["username"] == username:
                u["password_hash"] = generate_password_hash(new_password)
                u["must_change_password"] = bool(must_change)
                found = True
                break
        if not found:
            raise ValueError(f"Пользователь '{username}' не найден")
        _write(data)


def delete_user(username):
    with _lock:
        data = _read()
        target = next((u for u in data if u["username"] == username), None)
        if not target:
            raise ValueError("Пользователь не найден")
        if target["role"] == "admin":
            admins = [u for u in data if u["role"] == "admin"]
            if len(admins) <= 1:
                raise ValueError("Нельзя удалить последнего администратора")
        data = [u for u in data if u["username"] != username]
        _write(data)


def ensure_default_admin():
    """Создаёт admin/admin при первом запуске (must_change=True)."""
    if count() == 0:
        create_user("admin", "admin", role="admin", must_change=True)
        print("[boot] Создан пользователь admin/admin (требуется смена пароля при первом входе)")