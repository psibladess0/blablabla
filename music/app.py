"""Сайт «Пересечение»: сравнение музыкальных вкусов по Яндекс Музыке.

Каждый человек вводит свой токен на сайте. Сервер один раз загружает его лайки,
сохраняет в памяти только названия (токен нигде не хранится) и сравнивает
участников комнаты.
"""
import re
import secrets
import string
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from yandex_music import Client

app = Flask(__name__, static_folder="static")

TTL = 24 * 3600          # комната живёт сутки
MAX_USERS = 8
LIMIT = 300              # сколько общих элементов отдавать в список
ROOMS = {}               # код -> {"created": ts, "users": {имя: библиотека}}
LOCK = threading.Lock()
LOGINS = {}              # id входа -> состояние (вход по коду без токена)


def norm(s):
    return re.sub(r"[\W_]+", " ", (s or "").lower().replace("ё", "е")).strip()


def fetch_library(token):
    client = Client(token).init()
    login = client.me.account.login or "user"
    lib = {"artists": {}, "tracks": {}, "albums": {}}

    def add(kind, key, name):
        if not key.replace("|", "").strip():
            return
        e = lib[kind].setdefault(key, {"name": name, "n": 0})
        e["n"] += 1

    likes = client.users_likes_tracks()
    for t in (likes.fetch_tracks() if likes else []):
        names = [a.name for a in t.artists]
        a0 = names[0] if names else ""
        for a in names:
            add("artists", norm(a), a)
        add("tracks", f"{norm(t.title)}|{norm(a0)}", f"{a0} — {t.title}" if a0 else t.title)
        if t.albums:
            title = t.albums[0].title
            add("albums", f"{norm(title)}|{norm(a0)}", f"{a0} — {title}" if a0 else title)

    try:
        for like in client.users_likes_artists():
            if like.artist and norm(like.artist.name) not in lib["artists"]:
                add("artists", norm(like.artist.name), like.artist.name)
    except Exception:
        pass
    try:
        for like in client.users_likes_albums():
            al = like.album
            if not al:
                continue
            a0 = al.artists[0].name if al.artists else ""
            key = f"{norm(al.title)}|{norm(a0)}"
            if key not in lib["albums"]:
                add("albums", key, f"{a0} — {al.title}" if a0 else al.title)
    except Exception:
        pass
    return login, lib


def compare(users):
    names = list(users)
    libs = [users[n] for n in names]

    def jac(a, b):
        A, B = set(a["artists"]), set(b["artists"])
        un = len(A | B)
        return len(A & B) / un if un else 0

    matrix = [[None if i == j else round(jac(a, b) * 100) for j, b in enumerate(libs)]
              for i, a in enumerate(libs)]
    common = {}
    for kind in ("artists", "tracks", "albums"):
        keys = set(libs[0][kind]) if libs else set()
        for lib in libs[1:]:
            keys &= set(lib[kind])
        rows = [{"name": libs[0][kind][k]["name"],
                 "per": [lib[kind][k]["n"] for lib in libs]} for k in keys]
        rows.sort(key=lambda r: -sum(r["per"]))
        common[kind] = {"total": len(rows), "items": rows[:LIMIT]}
    return {"users": [{"name": n, "tracks": len(users[n]["tracks"])} for n in names],
            "matrix": matrix, "common": common}


def get_room(code):
    now = time.time()
    with LOCK:
        for c in [c for c, r in ROOMS.items() if now - r["created"] > TTL]:
            del ROOMS[c]
        return ROOMS.get(code.upper())


def run_login(lid, room_code, ev):
    st = LOGINS[lid]

    def on_code(code):
        st["url"] = code.verification_url
        st["code"] = code.user_code
        ev.set()

    try:
        token = Client().device_auth(on_code=on_code)
        token = getattr(token, "access_token", token)
        login, lib = fetch_library(token)
        room = get_room(room_code)
        if not room:
            raise RuntimeError("room")
        with LOCK:
            room["users"][login] = lib
        st["status"] = "ok"
    except Exception:
        st["status"] = "error"
        st["error"] = "Вход не удался или время вышло. Попробуйте ещё раз."
    finally:
        ev.set()


@app.post("/api/rooms/<code>/login")
def start_login(code):
    room = get_room(code)
    if not room:
        return jsonify(error="Комната не найдена или устарела"), 404
    lid = secrets.token_urlsafe(8)
    ev = threading.Event()
    LOGINS[lid] = {"status": "wait"}
    threading.Thread(target=run_login, args=(lid, code.upper(), ev), daemon=True).start()
    ev.wait(20)
    st = LOGINS[lid]
    if st["status"] == "error" or "code" not in st:
        LOGINS.pop(lid, None)
        return jsonify(error="Не удалось начать вход через Яндекс. Вставьте токен вручную."), 400
    return jsonify(id=lid, url=st["url"], code=st["code"])


@app.get("/api/login/<lid>")
def login_state(lid):
    st = LOGINS.get(lid)
    if not st:
        return jsonify(error="Вход не найден"), 404
    if st["status"] != "wait":
        LOGINS.pop(lid, None)
    return jsonify(status=st["status"], error=st.get("error"))


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/rooms")
def create_room():
    code = "".join(secrets.choice(string.ascii_uppercase) for _ in range(6))
    with LOCK:
        ROOMS[code] = {"created": time.time(), "users": {}}
    return jsonify(code=code)


@app.post("/api/rooms/<code>/join")
def join(code):
    room = get_room(code)
    if not room:
        return jsonify(error="Комната не найдена или устарела"), 404
    token = ((request.get_json(silent=True) or {}).get("token") or "").strip()
    if not token:
        return jsonify(error="Введите токен"), 400
    try:
        login, lib = fetch_library(token)
    except Exception:
        return jsonify(error="Не удалось войти в Яндекс Музыку. Проверьте токен."), 400
    with LOCK:
        if login not in room["users"] and len(room["users"]) >= MAX_USERS:
            return jsonify(error="В комнате уже максимум участников"), 400
        room["users"][login] = lib
    return jsonify(user=login, tracks=len(lib["tracks"]))


@app.get("/api/rooms/<code>")
def room_state(code):
    room = get_room(code)
    if not room:
        return jsonify(error="Комната не найдена или устарела"), 404
    with LOCK:
        users = dict(room["users"])
    return jsonify(compare(users))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
