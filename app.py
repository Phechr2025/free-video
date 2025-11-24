
import os
import sqlite3
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_file, abort
)

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

DB_PATH = "videos.db"
BASE_DIR = os.path.dirname(__file__)
VIDEO_ROOT = os.path.join(BASE_DIR, "video_files")
COVER_ROOT = os.path.join(BASE_DIR, "static", "covers")
EPISODE_COVER_ROOT = os.path.join(COVER_ROOT, "episodes")

os.makedirs(VIDEO_ROOT, exist_ok=True)
os.makedirs(COVER_ROOT, exist_ok=True)
os.makedirs(EPISODE_COVER_ROOT, exist_ok=True)


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

    return render_template("watch.html", series=series, episode=episode)


@app.route("/stream/<int:episode_id>")
def stream_episode(episode_id):
    conn = get_db_connection()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    conn.close()

    if episode is None:
        abort(404)

    file_path = episode["file_path"]
    if not file_path:
        abort(404)

    if not os.path.isabs(file_path):
        file_path = os.path.join(BASE_DIR, file_path)

    if not os.path.exists(file_path):
        abort(404)

    return send_file(file_path, mimetype="video/mp4", as_attachment=False)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
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

        # ถ้าไม่อัปโหลดไฟล์ แต่ใส่ลิงก์ใหม่ ให้ใช้ลิงก์นั้นแทน (ไม่ลบไฟล์เก่า เผื่อยังใช้ที่อื่น)
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
                series_id, title, description, episode_number,
                source_type, video_url, drive_id, file_path,
                thumbnail_url, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
