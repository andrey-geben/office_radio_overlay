# auth.py
from functools import wraps
from flask import session, redirect, url_for, request, jsonify
import users_store


def _load_current_user():
    username = session.get("username")
    if not username:
        return None
    return users_store.get_user(username)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))

        user = _load_current_user()
        if not user:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "user_not_found"}), 401
            return redirect(url_for("login"))

        if user.get("must_change_password"):
            allowed = ("/change-password", "/logout")
            if request.path not in allowed and not request.path.startswith("/static/"):
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "password_change_required"}), 403
                return redirect(url_for("change_password"))

        # Обновляем роль в сессии — читаем актуальное значение
        session["role"] = user.get("role", "operator")
        session["must_change_password"] = bool(user.get("must_change_password", False))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))

        user = _load_current_user()
        if not user:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "user_not_found"}), 401
            return redirect(url_for("login"))

        if user.get("must_change_password"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "password_change_required"}), 403
            return redirect(url_for("change_password"))

        if user.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "forbidden"}), 403
            return redirect(url_for("index"))

        session["role"] = "admin"
        session["must_change_password"] = False
        return f(*args, **kwargs)
    return wrapper