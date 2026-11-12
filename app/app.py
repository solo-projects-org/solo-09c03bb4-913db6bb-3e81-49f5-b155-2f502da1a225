"""林道巡护倒木采集系统（Flask + SQLite）

- /            采集页（巡护员）：离线可用，待传记录存浏览器 IndexedDB
- /records     清障队查看页：只展示已确认入库的记录
- /api/records GET 列表 / POST 补传（uuid 幂等，同一笔只入库一次）
- /photos/...  已入库的原照
"""
import os
import sqlite3
import threading
import uuid as uuidlib
from datetime import datetime, timezone

from flask import Flask, g, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
# 补传过程中先写到暂存目录，只有入库成功后才原子移动到 PHOTO_DIR。
# 这样崩溃/入库失败都不会在“已入库原照”目录里留下半成品。
STAGING_DIR = os.path.join(DATA_DIR, "staging")
DB_PATH = os.path.join(DATA_DIR, "records.db")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 正在写盘的暂存文件（本进程内），清理时绝不能误删这些“仍在补传”的照片。
_active_lock = threading.Lock()
_active_staging = set()
# 串行化“入库 + 移动照片”与启动清理，避免清理删掉刚提交的照片。
_photo_state_lock = threading.RLock()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 原照最大 30MB

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT NOT NULL UNIQUE,   -- 浏览器生成的幂等键
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    photo       TEXT NOT NULL,          -- photos 目录下的文件名
    created_at  TEXT NOT NULL,          -- 现场采集时间（浏览器上报）
    uploaded_at TEXT NOT NULL           -- 入库时间（服务器）
);
"""


def init_db():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    os.makedirs(STAGING_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _rollback(db):
    try:
        db.rollback()
    except sqlite3.Error:
        pass


def _staging_final_name(name):
    """暂存文件名格式为 <随机后缀>.<最终文件名>，取回最终文件名。"""
    _token, sep, final = name.partition(".")
    return final if sep and final else None


def _is_active_staging(path):
    with _active_lock:
        return path in _active_staging


def _promote_staged_photo(final_name):
    """把已入库记录的暂存照片移动为正式原照（崩溃发生在入库后、移动前时用）。"""
    if not os.path.isdir(STAGING_DIR):
        return False
    for name in os.listdir(STAGING_DIR):
        if _staging_final_name(name) != final_name:
            continue
        src = os.path.join(STAGING_DIR, name)
        if not os.path.isfile(src) or _is_active_staging(src):
            continue
        dst = os.path.join(PHOTO_DIR, final_name)
        try:
            os.replace(src, dst)
            return True
        except OSError:
            return False
    return False


def cleanup_orphan_photos():
    """清理未入库的补传照片。

    - 暂存文件：已入库的恢复为正式原照；没有入库记录的删除。
    - 正式照片目录：只保留 records 表里引用的文件（兼容旧版本直接写盘
      留下的孤儿文件）。
    - 正在本进程补传（仍在写盘）的暂存文件绝不删除。
    """
    os.makedirs(PHOTO_DIR, exist_ok=True)
    os.makedirs(STAGING_DIR, exist_ok=True)

    with _photo_state_lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            committed = {row[0] for row in conn.execute("SELECT photo FROM records")}
        finally:
            conn.close()

        # 暂存区：入库成功的恢复，未入库的删除。
        for name in os.listdir(STAGING_DIR):
            src = os.path.join(STAGING_DIR, name)
            if not os.path.isfile(src):
                continue
            final = _staging_final_name(name)
            if final and final in committed:
                dst = os.path.join(PHOTO_DIR, final)
                if not os.path.exists(dst):
                    try:
                        os.replace(src, dst)
                        continue
                    except OSError:
                        pass
                else:
                    _safe_remove(src)
            elif not _is_active_staging(src):
                _safe_remove(src)

        # 照片目录：没有入库记录引用的都是孤儿（含旧版本崩溃残留）。
        for name in os.listdir(PHOTO_DIR):
            path = os.path.join(PHOTO_DIR, name)
            if not os.path.isfile(path) or name in committed:
                continue
            if _is_active_staging(path):
                continue
            _safe_remove(path)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def no_store_html(resp):
    # HTML 页面不做 HTTP 缓存，保证在线时拿到最新数据；
    # 离线可用性由 Service Worker 的 Cache Storage 负责。
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- 页面 ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    # Service Worker 必须挂在根作用域下：从 / 路径提供，控制整个站点
    resp = send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js",
                               mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/records")
def records_page():
    rows = get_db().execute(
        "SELECT * FROM records ORDER BY id DESC"
    ).fetchall()
    return render_template("records.html", records=rows)


# ---------------- API ----------------

@app.route("/api/records", methods=["GET"])
def api_list():
    rows = get_db().execute(
        "SELECT id, uuid, lat, lng, note, photo, created_at, uploaded_at "
        "FROM records ORDER BY id DESC"
    ).fetchall()
    return jsonify([
        {
            "id": r["id"],
            "uuid": r["uuid"],
            "lat": r["lat"],
            "lng": r["lng"],
            "note": r["note"],
            "photo_url": "/photos/" + r["photo"],
            "created_at": r["created_at"],
            "uploaded_at": r["uploaded_at"],
        }
        for r in rows
    ])


@app.route("/api/records", methods=["POST"])
def api_create():
    # ---- 校验 ----
    client_uuid = (request.form.get("uuid") or "").strip()
    try:
        client_uuid = str(uuidlib.UUID(client_uuid))  # 规范化
    except (ValueError, AttributeError):
        return jsonify({"ok": False, "error": "uuid 缺失或格式非法"}), 400

    try:
        lat = float(request.form.get("lat", ""))
        lng = float(request.form.get("lng", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "经纬度缺失或非法"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"ok": False, "error": "经纬度超出范围"}), 400

    note = (request.form.get("note") or "").strip()[:500]
    created_at = (request.form.get("created_at") or "").strip() or \
        datetime.now(timezone.utc).isoformat()

    photo = request.files.get("photo")
    if photo is None or not photo.filename:
        return jsonify({"ok": False, "error": "缺少现场照片"}), 400

    db = get_db()

    # ---- 幂等：已入库则直接确认，不重复写入 ----
    existing = db.execute(
        "SELECT id, photo FROM records WHERE uuid = ?", (client_uuid,)
    ).fetchone()
    if existing:
        # 极端情况下上次入库后、照片移动前进程退出：尝试找回暂存原照。
        if not os.path.exists(os.path.join(PHOTO_DIR, existing["photo"])):
            with _photo_state_lock:
                _promote_staged_photo(existing["photo"])
        return jsonify({"ok": True, "duplicate": True, "id": existing["id"]})

    # ---- 原照先写暂存文件；入库成功后才移动到 photos/ ----
    # 文件名用 uuid，天然去重；暂存名额外加随机后缀，避免并发重试互相覆盖。
    ext = os.path.splitext(secure_filename(photo.filename))[1].lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    fname = client_uuid + ext
    final_path = os.path.join(PHOTO_DIR, fname)
    staging_path = os.path.join(STAGING_DIR, uuidlib.uuid4().hex + "." + fname)

    with _active_lock:
        _active_staging.add(staging_path)
    try:
        try:
            photo.save(staging_path)
        except Exception:
            _safe_remove(staging_path)
            app.logger.exception("原照写盘失败")
            return jsonify({"ok": False, "error": "原照保存失败，请稍后重试"}), 500

        # ---- 入库；唯一约束兜底并发/重试竞争 ----
        with _photo_state_lock:
            try:
                cur = db.execute(
                    "INSERT INTO records (uuid, lat, lng, note, photo, created_at, uploaded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (client_uuid, lat, lng, note, fname, created_at,
                     datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
            except sqlite3.IntegrityError:
                # 并发的同一笔已由另一请求入库：只丢弃本次的暂存照片，
                # 绝不删除对方已入库的原照。
                _rollback(db)
                _safe_remove(staging_path)
                row = db.execute(
                    "SELECT id, photo FROM records WHERE uuid = ?", (client_uuid,)
                ).fetchone()
                if row is not None:
                    _promote_staged_photo(row["photo"])
                    return jsonify({"ok": True, "duplicate": True,
                                    "id": row["id"]})
                # 不是幂等冲突，而是其它约束/写入失败：按入库失败处理。
                app.logger.exception("记录入库冲突")
                return jsonify({"ok": False, "error": "记录入库失败，请稍后重试"}), 500
            except Exception:
                # 入库失败：清掉本次留下的暂存照片，浏览器会保留待传记录重试。
                _rollback(db)
                _safe_remove(staging_path)
                app.logger.exception("记录入库失败")
                return jsonify({"ok": False, "error": "记录入库失败，请稍后重试"}), 500

            try:
                os.replace(staging_path, final_path)
            except OSError:
                # 已入库但移动失败：保留暂存文件，下次启动清理时会恢复；
                # 此时对浏览器仍算确认成功，避免重复补传。
                app.logger.exception("原照移入 photos 目录失败")
            return jsonify({"ok": True, "duplicate": False, "id": cur.lastrowid})
    finally:
        with _active_lock:
            _active_staging.discard(staging_path)


@app.route("/photos/<path:name>")
def photo_file(name):
    # 只允许打开已入库记录引用的原照；未入库的暂存/孤儿照片一律 404。
    row = get_db().execute(
        "SELECT 1 FROM records WHERE photo = ?", (name,)
    ).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "照片不存在"}), 404

    # 崩溃发生在入库成功、原照移动前时，先从暂存恢复再提供访问。
    if not os.path.isfile(os.path.join(PHOTO_DIR, name)):
        with _photo_state_lock:
            _promote_staged_photo(name)
    return send_from_directory(PHOTO_DIR, name)


init_db()
cleanup_orphan_photos()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
