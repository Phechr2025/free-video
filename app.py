
import os
import sqlite3
import json
from datetime import datetime
from io import BytesIO
import re

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_file, abort, Response
)

app = Flask(__name__)

# ---------- Admin login defaults (reset every restart) ----------
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "1234"

# ค่า login ปัจจุบันในหน่วยความจำ (รีเซ็ตเมื่อรีสตาร์ท)
current_admin_username = DEFAULT_ADMIN_USERNAME
current_admin_password = DEFAULT_ADMIN_PASSWORD

DB_PATH = "videos.db"
BASE_DIR = os.path.dirname(__file__)
VIDEO_ROOT = os.path.join(BASE_DIR, "video_files")
COVER_ROOT = os.path.join(BASE_DIR, "static", "covers")
EPISODE_COVER_ROOT = os.path.join(COVER_ROOT, "episodes")

os.makedirs(VIDEO_ROOT, exist_ok=True)
os.makedirs(COVER_ROOT, exist_ok=True)
os.makedirs(EPISODE_COVER_ROOT, exist_ok=True)

# ใช้ secret key แบบง่าย ๆ ถ้ายังไม่ตั้งค่า
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def ensure_episode_thumbnail_column(conn: sqlite3.Connection):
    """เพิ่มคอลัมน์ thumbnail_url ให้ตาราง episodes ถ้ายังไม่มี (ใช้ตอนอัปเดตจากเวอร์ชันเก่า)."""
    cur = conn.execute("PRAGMA table_info(episodes)")
    cols = [row[1] for row in cur.fetchall()]
    if "thumbnail_url" not in cols:
        conn.execute("ALTER TABLE episodes ADD COLUMN thumbnail_url TEXT")
        conn.commit()




def ensure_visibility_columns(conn: sqlite3.Connection):
    """เพิ่มคอลัมน์ is_active ให้ตาราง series และ episodes ถ้ายังไม่มี (ใช้เปิด/ปิดการดู)."""
    # ตาราง series
    cur = conn.execute("PRAGMA table_info(series)")
    cols = [row[1] for row in cur.fetchall()]
    if "is_active" not in cols:
        conn.execute("ALTER TABLE series ADD COLUMN is_active INTEGER DEFAULT 1")
    # ตาราง episodes
    cur = conn.execute("PRAGMA table_info(episodes)")
    cols = [row[1] for row in cur.fetchall()]
    if "is_active" not in cols:
        conn.execute("ALTER TABLE episodes ADD COLUMN is_active INTEGER DEFAULT 1")
    conn.commit()

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    # ตารางเรื่อง
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            thumbnail_url TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    # ตารางตอน (เวอร์ชันใหม่มี thumbnail_url)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            episode_number INTEGER,
            source_type TEXT NOT NULL,
            video_url TEXT,
            drive_id TEXT,
            file_path TEXT,
            thumbnail_url TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(series_id) REFERENCES series(id) ON DELETE CASCADE
        )
        """
    )

    # กรณีอัปเกรดจากเวอร์ชันเก่าที่ไม่มีคอลัมน์ thumbnail_url
    ensure_episode_thumbnail_column(conn)
    ensure_visibility_columns(conn)

    conn.commit()
    conn.close()


init_db()


def extract_drive_id(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None

    if "drive.google.com" not in text:
        return text

    if "/file/d/" in text:
        try:
            part = text.split("/file/d/")[1]
            file_id = part.split("/")[0]
            return file_id
        except Exception:
            pass

    if "id=" in text:
        try:
            part = text.split("id=")[1]
            file_id = part.split("&")[0]
            return file_id
        except Exception:
            pass

    return None


def download_drive_file(file_id: str, series_id: int) -> str:
    import gdown

    series_dir = os.path.join(VIDEO_ROOT, f"series_{series_id}")
    os.makedirs(series_dir, exist_ok=True)

    output = os.path.join(series_dir, f"{file_id}.mp4")

    if os.path.exists(output):
        return output

    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        gdown.download(url, output, quiet=False)
    except Exception as e:
        raise RuntimeError(f"โหลดไฟล์จาก Google Drive ไม่สำเร็จ: {e}")

    if not os.path.exists(output):
        raise RuntimeError("ไม่พบไฟล์ที่ดาวน์โหลดจาก Google Drive")

    return output


def is_admin() -> bool:
    return bool(session.get("is_admin"))


def admin_required():
    if not is_admin():
        flash("ต้องเข้าสู่ระบบแอดมินก่อน", "error")
        return False
    return True


@app.route("/")
def index():
    conn = get_db_connection()
    series_list = conn.execute(
        "SELECT * FROM series ORDER BY datetime(created_at) DESC"
    ).fetchall()
    conn.close()
    return render_template("index.html", series_list=series_list)




@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return redirect(url_for("index"))

    # ตัดคำอย่างง่าย: เอาคำหลัก เช่น "มหาเวทย์ผนึกมาร" จาก "มหาเวทย์ผนึกมาร S2"
    tokens = query.split()
    keywords = [t for t in tokens if not re.fullmatch(r"[sS]\d+", t)]
    main_keyword = max(keywords, key=len) if keywords else query

    conn = get_db_connection()
    series_rows = conn.execute("SELECT * FROM series").fetchall()
    conn.close()

    def score(row):
        title = (row["title"] or "").lower()
        desc = (row["description"] or "").lower()
        q = query.lower()
        mk = main_keyword.lower()

        if title == q:
            base = 4
        elif q in title:
            base = 3
        elif mk in title:
            base = 2
        elif mk in desc:
            base = 1
        else:
            base = 0

        return base

    sorted_rows = sorted(series_rows, key=score, reverse=True)
    results = sorted_rows  # แสดงทุกเรื่อง แต่จัดอันดับให้เรื่องที่ตรงสุดอยู่ด้านบน

    return render_template(
        "search_results.html",
        query=query,
        main_keyword=main_keyword,
        series_list=results,
    )

@app.route("/series/<int:series_id>")
def series_detail(series_id):
    conn = get_db_connection()
    series = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()
    if series is None:
        conn.close()
        flash("ไม่พบเรื่องนี้", "error")
        return redirect(url_for("index"))

    episodes = conn.execute(
        """
        SELECT * FROM episodes
        WHERE series_id = ?
        ORDER BY episode_number IS NULL, episode_number, datetime(created_at)
        """,
        (series_id,),
    ).fetchall()
    conn.close()
    return render_template("series_detail.html", series=series, episodes=episodes)


@app.route("/series/<int:series_id>/episode/<int:episode_id>")
def watch_episode(series_id, episode_id):
    conn = get_db_connection()
    series = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id = ? AND series_id = ?",
        (episode_id, series_id),
    ).fetchone()
    conn.close()

    if series is None or episode is None:
        flash("ไม่พบตอนนี้", "error")
        return redirect(url_for("index"))

    # ตรวจสอบสถานะเปิด/ปิด
    series_active = 1
    try:
        if "is_active" in series.keys() and series["is_active"] is not None:
            series_active = int(series["is_active"])
    except Exception:
        series_active = 1

    episode_active = 1
    try:
        if "is_active" in episode.keys() and episode["is_active"] is not None:
            episode_active = int(episode["is_active"])
    except Exception:
        episode_active = 1

    blocked = (series_active == 0) or (episode_active == 0)

    # ผู้ใช้ยังเข้าได้ปกติ แต่ถ้า blocked == True จะขึ้นข้อความในหน้า watch.html แทนวิดีโอ
    return render_template("watch.html", series=series, episode=episode, blocked=blocked)


@app.route("/stream/<int:episode_id>")
def stream_episode(episode_id):
    conn = get_db_connection()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()

    series = None
    if episode is not None:
        series = conn.execute(
            "SELECT * FROM series WHERE id = ?",
            (episode["series_id"],),
        ).fetchone()

    conn.close()

    if episode is None or series is None:
        abort(404)

    # ถ้าเรื่องหรืออตอนถูกปิด จะไม่ให้สตรีมวิดีโอ
    series_active = 1
    try:
        if "is_active" in series.keys() and series["is_active"] is not None:
            series_active = int(series["is_active"])
    except Exception:
        series_active = 1

    episode_active = 1
    try:
        if "is_active" in episode.keys() and episode["is_active"] is not None:
            episode_active = int(episode["is_active"])
    except Exception:
        episode_active = 1

    if series_active == 0 or episode_active == 0:
        abort(403)

    # ---------------------------
    # เตรียม path ของไฟล์วิดีโอ
    # ถ้าไฟล์หายไป (เช่น ย้ายเซิร์ฟเวอร์/รีดีพลอยใหม่)
    # และเป็นตอนแบบ Google Drive ให้ลองโหลดใหม่อัตโนมัติ
    # ---------------------------
    file_path = episode["file_path"]
    if file_path and not os.path.isabs(file_path):
        abs_path = os.path.join(BASE_DIR, file_path)
    else:
        abs_path = file_path

    if not abs_path or not os.path.exists(abs_path):
        # ลองดาวน์โหลดใหม่จาก Google Drive ถ้าเป็นตอนโหมด gdrive
        source_type = None
        drive_id = None
        try:
            if "source_type" in episode.keys():
                source_type = episode["source_type"]
            if "drive_id" in episode.keys():
                drive_id = episode["drive_id"]
        except Exception:
            source_type = None
            drive_id = None

        if source_type == "gdrive" and drive_id:
            try:
                # ดาวน์โหลดไฟล์ใหม่
                new_file = download_drive_file(drive_id, episode["series_id"])
                # เก็บ path แบบ relative ลง DB เพื่อใช้ครั้งต่อไป
                rel_path = os.path.relpath(new_file, BASE_DIR)
                conn2 = get_db_connection()
                conn2.execute(
                    "UPDATE episodes SET file_path = ? WHERE id = ?",
                    (rel_path, episode["id"]),
                )
                conn2.commit()
                conn2.close()
                abs_path = new_file
            except Exception:
                abort(404)
        else:
            abort(404)

    return send_file(abs_path, mimetype="video/mp4", as_attachment=False)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    global current_admin_username, current_admin_password

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == current_admin_username and password == current_admin_password:
            session["is_admin"] = True
            session["admin_username"] = username
            flash("เข้าสู่ระบบแอดมินสำเร็จ", "success")
            return redirect(url_for("admin_series"))
        else:
            flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")

    return render_template("login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    session.pop("admin_username", None)
    flash("ออกจากระบบแล้ว", "info")
    return redirect(url_for("index"))


@app.route("/admin/account", methods=["GET", "POST"])
def admin_account():
    global current_admin_username, current_admin_password

    if not admin_required():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        new_username = request.form.get("new_username", "").strip()
        new_password = request.form.get("new_password", "").strip()

        if not new_username or not new_password:
            flash("กรุณากรอกทั้งชื่อผู้ใช้ใหม่และรหัสผ่านใหม่", "error")
        else:
            current_admin_username = new_username
            current_admin_password = new_password
            flash("เปลี่ยนชื่อผู้ใช้และรหัสผ่านแอดมินสำเร็จ (มีผลจนกว่าจะรีสตาร์ท)", "success")
            return redirect(url_for("admin_account"))

    return render_template(
        "admin_account.html",
        current_username=current_admin_username,
        default_username=DEFAULT_ADMIN_USERNAME,
        default_password=DEFAULT_ADMIN_PASSWORD,
    )


@app.route("/admin/series", methods=["GET", "POST"])
def admin_series():
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        thumbnail_url_input = request.form.get("thumbnail_url", "").strip()
        cover_file = request.files.get("cover_file")

        if not title:
            flash("กรุณากรอกชื่อเรื่อง", "error")
        else:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO series (title, description, thumbnail_url, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (title, description, None, datetime.utcnow().isoformat()),
            )
            series_id = cur.lastrowid
            conn.commit()

            thumbnail_value = None

            if cover_file and cover_file.filename:
                filename = os.path.basename(cover_file.filename)
                base, ext = os.path.splitext(filename)
                ext = ext.lower() or ".jpg"

                series_cover_dir = os.path.join(COVER_ROOT, f"series_{series_id}")
                os.makedirs(series_cover_dir, exist_ok=True)

                safe_name = f"cover_{series_id}_{int(datetime.utcnow().timestamp())}{ext}"
                save_path = os.path.join(series_cover_dir, safe_name)
                cover_file.save(save_path)

                rel_path_from_static = f"covers/series_{series_id}/{safe_name}"
                thumbnail_value = rel_path_from_static

            elif thumbnail_url_input:
                thumbnail_value = thumbnail_url_input

            if thumbnail_value is not None:
                conn.execute(
                    "UPDATE series SET thumbnail_url = ? WHERE id = ?",
                    (thumbnail_value, series_id),
                )
                conn.commit()

            flash("เพิ่มเรื่องใหม่สำเร็จแล้ว", "success")

    series_list = conn.execute(
        "SELECT * FROM series ORDER BY datetime(created_at) DESC"
    ).fetchall()
    conn.close()
    return render_template("admin_series.html", series_list=series_list)




@app.route("/admin/series/<int:series_id>/toggle_visibility", methods=["POST"])
def admin_toggle_series(series_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()

    if row is None:
        conn.close()
        flash("ไม่พบเรื่องนี้", "error")
        return redirect(url_for("admin_series"))

    current = 1
    try:
        if "is_active" in row.keys() and row["is_active"] is not None:
            current = int(row["is_active"])
    except Exception:
        current = 1

    new_val = 0 if current == 1 else 1
    conn.execute(
        "UPDATE series SET is_active = ? WHERE id = ?",
        (new_val, series_id),
    )
    conn.commit()
    conn.close()

    flash("อัปเดตสถานะการเปิด/ปิดเรื่องเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_series"))

@app.route("/admin/series/<int:series_id>/edit", methods=["GET", "POST"])
def admin_edit_series(series_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    series = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()

    if series is None:
        conn.close()
        flash("ไม่พบเรื่องนี้", "error")
        return redirect(url_for("admin_series"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        thumbnail_url_input = request.form.get("thumbnail_url", "").strip()
        cover_file = request.files.get("cover_file")

        if not title:
            flash("กรุณากรอกชื่อเรื่อง", "error")
            return redirect(url_for("admin_edit_series", series_id=series_id))

        thumbnail_value = series["thumbnail_url"]

        # ถ้าอัปโหลดรูปใหม่ ให้ลบรูปเก่าที่เป็นไฟล์ใน static ออกก่อน
        if cover_file and cover_file.filename:
            if thumbnail_value and not str(thumbnail_value).startswith("http"):
                old_path = os.path.join(BASE_DIR, "static", thumbnail_value)
                try:
                    if os.path.exists(old_path):
                        os.remove(old_path)
                except Exception:
                    pass

            filename = os.path.basename(cover_file.filename)
            base, ext = os.path.splitext(filename)
            ext = ext.lower() or ".jpg"

            series_cover_dir = os.path.join(COVER_ROOT, f"series_{series_id}")
            os.makedirs(series_cover_dir, exist_ok=True)

            safe_name = f"cover_{series_id}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(series_cover_dir, safe_name)
            cover_file.save(save_path)

            rel_path_from_static = f"covers/series_{series_id}/{safe_name}"
            thumbnail_value = rel_path_from_static

        # ถ้าไม่อัปโหลดไฟล์ แต่ใส่ลิงก์ใหม่ ให้ใช้ลิงก์นั้นแทน
        elif thumbnail_url_input:
            thumbnail_value = thumbnail_url_input

        conn.execute(
            """
            UPDATE series
            SET title = ?, description = ?, thumbnail_url = ?
            WHERE id = ?
            """,
            (title, description, thumbnail_value, series_id),
        )
        conn.commit()

        flash("อัปเดตข้อมูลเรื่องเรียบร้อยแล้ว", "success")
        return redirect(url_for("admin_series"))

    conn.close()
    return render_template("admin_edit_series.html", series=series)


@app.route("/admin/series/<int:series_id>/delete", methods=["POST"])
def admin_delete_series(series_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    episodes = conn.execute(
        "SELECT file_path FROM episodes WHERE series_id = ?", (series_id,)
    ).fetchall()

    for ep in episodes:
        fp = ep["file_path"]
        if fp:
            if not os.path.isabs(fp):
                fp_full = os.path.join(BASE_DIR, fp)
            else:
                fp_full = fp
            try:
                if os.path.exists(fp_full):
                    os.remove(fp_full)
            except Exception:
                pass

    conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
    conn.commit()
    conn.close()

    series_dir = os.path.join(VIDEO_ROOT, f"series_{series_id}")
    if os.path.isdir(series_dir):
        try:
            import shutil
            shutil.rmtree(series_dir)
        except Exception:
            pass

    cover_dir = os.path.join(COVER_ROOT, f"series_{series_id}")
    if os.path.isdir(cover_dir):
        try:
            import shutil
            shutil.rmtree(cover_dir)
        except Exception:
            pass

    flash("ลบเรื่องและตอนทั้งหมดเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_series"))


@app.route("/admin/series/<int:series_id>/episodes", methods=["GET", "POST"])
def admin_episodes(series_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    series = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()
    if series is None:
        conn.close()
        flash("ไม่พบเรื่องนี้", "error")
        return redirect(url_for("admin_series"))

    if request.method == "POST":
        mode = request.form.get("mode", "direct")
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        episode_number_raw = request.form.get("episode_number", "").strip()
        thumbnail_url_input = request.form.get("thumbnail_url", "").strip()
        cover_file = request.files.get("cover_file")

        episode_number = int(episode_number_raw) if episode_number_raw.isdigit() else None

        if not title:
            flash("กรุณากรอกชื่อตอน", "error")
            return redirect(url_for("admin_episodes", series_id=series_id))

        source_type = None
        video_url = None
        drive_id = None
        file_path = None

        if mode == "direct":
            video_url = request.form.get("video_url", "").strip()
            if not video_url:
                flash("กรุณากรอกลิงก์วิดีโอแบบ mp4", "error")
                return redirect(url_for("admin_episodes", series_id=series_id))
            source_type = "direct"

        elif mode == "gdrive":
            drive_text = request.form.get("drive_link", "").strip()
            drive_id = extract_drive_id(drive_text)
            if not drive_id:
                flash("ไม่สามารถดึง Drive ID จากลิงก์ได้ กรุณาตรวจสอบอีกครั้ง", "error")
                return redirect(url_for("admin_episodes", series_id=series_id))

            try:
                file_real = download_drive_file(drive_id, series_id)
            except Exception as e:
                flash(str(e), "error")
                return redirect(url_for("admin_episodes", series_id=series_id))

            rel_path = os.path.relpath(file_real, BASE_DIR)
            file_path = rel_path
            source_type = "gdrive"

        elif mode == "upload":
            file = request.files.get("file")
            if not file or file.filename == "":
                flash("กรุณาเลือกไฟล์วิดีโอสำหรับอัปโหลด", "error")
                return redirect(url_for("admin_episodes", series_id=series_id))

            filename = os.path.basename(file.filename)
            base, ext = os.path.splitext(filename)
            ext = ext.lower() or ".mp4"

            series_dir = os.path.join(VIDEO_ROOT, f"series_{series_id}")
            os.makedirs(series_dir, exist_ok=True)

            safe_name = f"{base}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(series_dir, safe_name)
            file.save(save_path)

            rel_path = os.path.relpath(save_path, BASE_DIR)
            file_path = rel_path
            source_type = "upload"

        else:
            flash("โหมดที่เลือกไม่ถูกต้อง", "error")
            return redirect(url_for("admin_episodes", series_id=series_id))

        # ขั้นแรก เพิ่มตอนโดยยังไม่รู้ path ปก (thumbnail_url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO episodes (
                id, series_id, title, description, episode_number,
                source_type, video_url, drive_id, file_path,
                thumbnail_url, created_at
            )
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                series_id,
                title,
                description,
                episode_number,
                source_type,
                video_url,
                drive_id,
                file_path,
                None,
                datetime.utcnow().isoformat(),
            ),
        )
        episode_id = cur.lastrowid
        conn.commit()

        thumb_value = None

        if cover_file and cover_file.filename:
            filename = os.path.basename(cover_file.filename)
            base, ext = os.path.splitext(filename)
            ext = ext.lower() or ".jpg"

            ep_dir = os.path.join(EPISODE_COVER_ROOT, f"ep_{episode_id}")
            os.makedirs(ep_dir, exist_ok=True)

            safe_name = f"ep_{episode_id}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(ep_dir, safe_name)
            cover_file.save(save_path)

            thumb_value = f"covers/episodes/ep_{episode_id}/{safe_name}"

        elif thumbnail_url_input:
            thumb_value = thumbnail_url_input

        if thumb_value is not None:
            conn.execute(
                "UPDATE episodes SET thumbnail_url = ? WHERE id = ?",
                (thumb_value, episode_id),
            )
            conn.commit()

        flash("เพิ่มตอนใหม่สำเร็จแล้ว", "success")

    episodes = conn.execute(
        """
        SELECT * FROM episodes
        WHERE series_id = ?
        ORDER BY episode_number IS NULL, episode_number, datetime(created_at)
        """,
        (series_id,),
    ).fetchall()
    conn.close()

    return render_template(
        "admin_episodes.html", series=series, episodes=episodes
    )





@app.route("/admin/episodes/<int:episode_id>/toggle_visibility", methods=["POST"])
def admin_toggle_episode(episode_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    ep = conn.execute(
        "SELECT * FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()

    if ep is None:
        conn.close()
        flash("ไม่พบตอนนี้", "error")
        return redirect(url_for("admin_series"))

    series_id = ep["series_id"]

    current = 1
    try:
        if "is_active" in ep.keys() and ep["is_active"] is not None:
            current = int(ep["is_active"])
    except Exception:
        current = 1

    new_val = 0 if current == 1 else 1
    conn.execute(
        "UPDATE episodes SET is_active = ? WHERE id = ?",
        (new_val, episode_id),
    )
    conn.commit()
    conn.close()

    flash("อัปเดตสถานะการเปิด/ปิดตอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_episodes", series_id=series_id))

@app.route("/admin/episodes/<int:episode_id>/edit", methods=["GET", "POST"])
def admin_edit_episode(episode_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    ep = conn.execute(
        "SELECT * FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    if ep is None:
        conn.close()
        flash("ไม่พบตอนนี้", "error")
        return redirect(url_for("admin_series"))

    series = conn.execute(
        "SELECT * FROM series WHERE id = ?",
        (ep["series_id"],),
    ).fetchone()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        episode_number_raw = request.form.get("episode_number", "").strip()
        mode = request.form.get("mode", "keep")
        thumbnail_url_input = request.form.get("thumbnail_url", "").strip()
        cover_file = request.files.get("cover_file")

        if not title:
            flash("กรุณากรอกชื่อตอน", "error")
            conn.close()
            return redirect(url_for("admin_edit_episode", episode_id=episode_id))

        episode_number = None
        if episode_number_raw:
            try:
                episode_number = int(episode_number_raw)
            except ValueError:
                flash("เลขตอนต้องเป็นตัวเลข", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

        new_source_type = ep["source_type"]
        new_video_url = ep["video_url"]
        new_drive_id = ep["drive_id"]
        new_file_path = ep["file_path"]

        def delete_old_file(path):
            if not path:
                return
            if not os.path.isabs(path):
                fp_full = os.path.join(BASE_DIR, path)
            else:
                fp_full = path
            try:
                if os.path.exists(fp_full):
                    os.remove(fp_full)
            except Exception:
                pass

        if mode == "keep":
            pass
        elif mode == "direct":
            video_url = request.form.get("video_url", "").strip()
            if not video_url:
                flash("กรุณาใส่ลิงก์วิดีโอแบบ direct", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            if new_source_type in ("gdrive", "upload"):
                delete_old_file(new_file_path)
                new_file_path = None

            new_source_type = "direct"
            new_video_url = video_url
            new_drive_id = None

        elif mode == "gdrive":
            drive_link = request.form.get("drive_link", "").strip()
            if not drive_link:
                flash("กรุณาใส่ลิงก์หรือรหัสไฟล์ Google Drive", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            drive_id = extract_drive_id(drive_link)
            if not drive_id:
                flash("ไม่สามารถดึง Drive ID จากลิงก์ได้ กรุณาตรวจสอบอีกครั้ง", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            if new_source_type in ("gdrive", "upload"):
                delete_old_file(new_file_path)

            try:
                file_real = download_drive_file(drive_id, ep["series_id"])
            except Exception as e:
                flash(str(e), "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            rel_path = os.path.relpath(file_real, BASE_DIR)
            new_file_path = rel_path
            new_source_type = "gdrive"
            new_drive_id = drive_id
            new_video_url = None

        elif mode == "upload":
            file = request.files.get("file")
            if not file or file.filename == "":
                flash("กรุณาเลือกไฟล์วิดีโอสำหรับอัปโหลด", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            if new_source_type in ("gdrive", "upload"):
                delete_old_file(new_file_path)

            filename = os.path.basename(file.filename)
            base, ext = os.path.splitext(filename)
            ext = ext.lower() or ".mp4"

            series_dir = os.path.join(VIDEO_ROOT, f"series_{ep['series_id']}")
            os.makedirs(series_dir, exist_ok=True)

            safe_name = f"{base}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(series_dir, safe_name)
            file.save(save_path)

            rel_path = os.path.relpath(save_path, BASE_DIR)
            new_file_path = rel_path
            new_source_type = "upload"
            new_video_url = None
            new_drive_id = None
        else:
            flash("โหมดที่เลือกไม่ถูกต้อง", "error")
            conn.close()
            return redirect(url_for("admin_edit_episode", episode_id=episode_id))

        conn.execute(
            """
            UPDATE episodes
            SET title = ?, description = ?, episode_number = ?, source_type = ?, video_url = ?, drive_id = ?, file_path = ?
            WHERE id = ?
            """,
            (
                title,
                description or None,
                episode_number,
                new_source_type,
                new_video_url,
                new_drive_id,
                new_file_path,
                episode_id,
            ),
        )

        thumb_value = None
        old_thumb = ep["thumbnail_url"]

        if cover_file and cover_file.filename:
            if old_thumb and not str(old_thumb).startswith("http"):
                if not os.path.isabs(old_thumb):
                    old_full = os.path.join(BASE_DIR, "static", old_thumb)
                else:
                    old_full = old_thumb
                try:
                    if os.path.exists(old_full):
                        os.remove(old_full)
                except Exception:
                    pass

            filename = os.path.basename(cover_file.filename)
            base2, ext2 = os.path.splitext(filename)
            ext2 = ext2.lower() or ".jpg"

            ep_dir = os.path.join(EPISODE_COVER_ROOT, f"ep_{episode_id}")
            os.makedirs(ep_dir, exist_ok=True)

            safe_name2 = f"ep_{episode_id}_{int(datetime.utcnow().timestamp())}{ext2}"
            save_path2 = os.path.join(ep_dir, safe_name2)
            cover_file.save(save_path2)

            thumb_value = f"covers/episodes/ep_{episode_id}/{safe_name2}"
        elif thumbnail_url_input:
            thumb_value = thumbnail_url_input

        if thumb_value is not None:
            conn.execute(
                "UPDATE episodes SET thumbnail_url = ? WHERE id = ?",
                (thumb_value, episode_id),
            )

        conn.commit()
        conn.close()
        flash("บันทึกการแก้ไขตอนเรียบร้อยแล้ว", "success")
        return redirect(url_for("admin_episodes", series_id=ep["series_id"]))

    conn.close()
    return render_template("admin_edit_episode.html", series=series, episode=ep)

@app.route("/admin/episodes/<int:episode_id>/delete", methods=["POST"])
def admin_delete_episode(episode_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    ep = conn.execute(
        "SELECT id, series_id, file_path, thumbnail_url FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    if ep is None:
        conn.close()
        flash("ไม่พบตอนนี้", "error")
        return redirect(url_for("admin_series"))

    file_path = ep["file_path"]
    thumb = ep["thumbnail_url"]
    series_id = ep["series_id"]

    if file_path:
        if not os.path.isabs(file_path):
            fp_full = os.path.join(BASE_DIR, file_path)
        else:
            fp_full = file_path
        try:
            if os.path.exists(fp_full):
                os.remove(fp_full)
        except Exception:
            pass

    # ลบไฟล์ปกตอนถ้าเป็นไฟล์ใน static
    if thumb and not str(thumb).startswith("http"):
        thumb_full = os.path.join(BASE_DIR, "static", thumb)
        try:
            if os.path.exists(thumb_full):
                os.remove(thumb_full)
            # ลบโฟลเดอร์เปล่า ep_... ด้วย
            ep_dir = os.path.dirname(thumb_full)
            if os.path.isdir(ep_dir) and not os.listdir(ep_dir):
                os.rmdir(ep_dir)
        except Exception:
            pass

    conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    conn.commit()
    conn.close()

    flash("ลบตอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_episodes", series_id=series_id))


# ---------- ระบบสำรอง/คืนค่า ----------
@app.route("/admin/backup", methods=["GET", "POST"])
def admin_backup():
    if not admin_required():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        file = request.files.get("backup_file")
        if not file or not file.filename:
            flash("กรุณาเลือกไฟล์สำรอง (.json) ก่อน", "error")
            return redirect(url_for("admin_backup"))

        try:
            data = json.load(file.stream)
        except Exception:
            flash("ไฟล์ไม่อยู่ในรูปแบบ JSON ที่ถูกต้อง", "error")
            return redirect(url_for("admin_backup"))

        series_list = data.get("series", [])
        episodes_list = data.get("episodes", [])

        conn = get_db_connection()
        ensure_episode_thumbnail_column(conn)
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys = OFF;")
        cur.execute("DELETE FROM episodes")
        cur.execute("DELETE FROM series")
        try:
            cur.execute("DELETE FROM sqlite_sequence WHERE name IN ('series','episodes')")
        except Exception:
            pass

        for s in series_list:
            cur.execute(
                """
                INSERT INTO series (id, title, description, thumbnail_url, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    s.get("id"),
                    s.get("title"),
                    s.get("description"),
                    s.get("thumbnail_url"),
                    s.get("created_at") or datetime.utcnow().isoformat(),
                ),
            )

        for ep in episodes_list:
            cur.execute(
                """
                INSERT INTO episodes (
                    id, series_id, title, description, episode_number,
                    source_type, video_url, drive_id, file_path,
                    thumbnail_url, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ep.get("id"),
                    ep.get("series_id"),
                    ep.get("title"),
                    ep.get("description"),
                    ep.get("episode_number"),
                    ep.get("source_type"),
                    ep.get("video_url"),
                    ep.get("drive_id"),
                    ep.get("file_path"),
                    ep.get("thumbnail_url"),
                    ep.get("created_at") or datetime.utcnow().isoformat(),
                ),
            )

        conn.commit()
        conn.close()

        flash("คืนค่าข้อมูลจากไฟล์สำเร็จแล้ว", "success")
        return redirect(url_for("admin_series"))

    return render_template("admin_backup.html")


@app.route("/admin/backup/download")
def admin_backup_download():
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    series = conn.execute("SELECT * FROM series").fetchall()
    episodes = conn.execute("SELECT * FROM episodes").fetchall()
    conn.close()

    data = {
        "version": "myseries_backup_v1",
        "exported_at": datetime.utcnow().isoformat(),
        "series": [dict(row) for row in series],
        "episodes": [dict(row) for row in episodes],
    }

    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"myseries_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        json_bytes,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)