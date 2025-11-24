
import os
import sqlite3
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_file, abort
)

# ---------------- Basic Config ----------------

app = Flask(__name__)

# ใช้ ENV บน Render ถ้ามี ไม่งั้นใช้ค่า dev
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

DB_PATH = "videos.db"
VIDEO_DIR = os.path.join(os.path.dirname(__file__), "video_files")
os.makedirs(VIDEO_DIR, exist_ok=True)


# ---------------- DB Helpers ----------------

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            source_type TEXT NOT NULL, -- direct / gdrive / upload
            video_url TEXT,            -- สำหรับ direct หรือเก็บ URL ต้นฉบับ
            drive_id TEXT,             -- ถ้าเป็นไฟล์จาก Google Drive
            file_path TEXT,            -- path ไฟล์ในเซิร์ฟเวอร์ (upload + gdrive)
            thumbnail_url TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


@app.before_first_request
def setup():
    init_db()


# ---------------- Util: Google Drive ----------------

def extract_drive_id(text: str) -> str | None:
    """
    รับเป็นลิงก์ Google Drive หรือ id ตรง ๆ
    แล้วคืนค่า file_id ถ้าดึงได้
    """
    text = (text or "").strip()
    if not text:
        return None

    # ถ้าไม่มี "drive.google.com" เดาว่าเป็น id ตรง ๆ
    if "drive.google.com" not in text:
        return text

    # รูปแบบ /file/d/<id>/
    if "/file/d/" in text:
        try:
            part = text.split("/file/d/")[1]
            file_id = part.split("/")[0]
            return file_id
        except Exception:
            pass

    # รูปแบบ ?id=<id>
    if "id=" in text:
        try:
            part = text.split("id=")[1]
            file_id = part.split("&")[0]
            return file_id
        except Exception:
            pass

    return None


def download_drive_file(file_id: str) -> str:
    """
    ดาวน์โหลดไฟล์จาก Google Drive มาเก็บใน VIDEO_DIR
    ใช้ gdown
    คืนค่า path ไฟล์ที่โหลดเสร็จ
    """
    import gdown

    os.makedirs(VIDEO_DIR, exist_ok=True)
    # ตั้งชื่อไฟล์จาก id เพื่อกันซ้ำง่าย ๆ
    output = os.path.join(VIDEO_DIR, f"{file_id}.mp4")

    # ถ้ามีไฟล์อยู่แล้ว ไม่ต้องโหลดซ้ำ
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


# ---------------- Auth Helpers ----------------

def is_admin() -> bool:
    return bool(session.get("is_admin"))


def admin_required():
    if not is_admin():
        flash("ต้องเข้าสู่ระบบแอดมินก่อน", "error")
        return False
    return True


# ---------------- Public Routes ----------------

@app.route("/")
def index():
    conn = get_db_connection()
    videos = conn.execute(
        "SELECT * FROM videos ORDER BY datetime(created_at) DESC"
    ).fetchall()
    conn.close()
    return render_template("index.html", videos=videos)


@app.route("/video/<int:video_id>")
def watch(video_id):
    conn = get_db_connection()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    conn.close()

    if video is None:
        flash("ไม่พบบันทึกวิดีโอนี้", "error")
        return redirect(url_for("index"))

    return render_template("watch.html", video=video)


@app.route("/stream/<int:video_id>")
def stream_video(video_id):
    conn = get_db_connection()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    conn.close()

    if video is None:
        abort(404)

    file_path = video["file_path"]
    if not file_path:
        abort(404)

    if not os.path.isabs(file_path):
        file_path = os.path.join(os.path.dirname(__file__), file_path)

    if not os.path.exists(file_path):
        abort(404)

    # ส่งไฟล์ mp4 กลับให้เล่น
    return send_file(file_path, mimetype="video/mp4", as_attachment=False)


# ---------------- Admin Login ----------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            flash("เข้าสู่ระบบแอดมินสำเร็จ", "success")
            return redirect(url_for("admin_upload"))
        else:
            flash("รหัสผ่านไม่ถูกต้อง", "error")
    return render_template("login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("ออกจากระบบแล้ว", "info")
    return redirect(url_for("index"))


# ---------------- Admin: Upload Page (A+B+C) ----------------

@app.route("/admin/upload", methods=["GET", "POST"])
def admin_upload():
    if not admin_required():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        mode = request.form.get("mode", "direct")
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        thumbnail_url = request.form.get("thumbnail_url", "").strip()

        if not title:
            flash("กรุณากรอกชื่อวิดีโอ", "error")
            return redirect(url_for("admin_upload"))

        source_type = None
        video_url = None
        drive_id = None
        file_path = None

        # ----- Mode A: ลิงก์ mp4 โดยตรง -----
        if mode == "direct":
            video_url = request.form.get("video_url", "").strip()
            if not video_url:
                flash("กรุณากรอกลิงก์วิดีโอแบบ mp4", "error")
                return redirect(url_for("admin_upload"))
            source_type = "direct"

        # ----- Mode B: Google Drive + gdown -----
        elif mode == "gdrive":
            drive_text = request.form.get("drive_link", "").strip()
            drive_id = extract_drive_id(drive_text)
            if not drive_id:
                flash("ไม่สามารถดึง Drive ID จากลิงก์ได้ กรุณาตรวจสอบอีกครั้ง", "error")
                return redirect(url_for("admin_upload"))

            try:
                file_path = download_drive_file(drive_id)
            except Exception as e:
                flash(str(e), "error")
                return redirect(url_for("admin_upload"))

            # เก็บ path แบบ relative เพื่อกัน path ผิดเมื่อ deploy
            rel_path = os.path.relpath(file_path, os.path.dirname(__file__))
            file_path = rel_path
            source_type = "gdrive"

        # ----- Mode C: อัปโหลดไฟล์ mp4 จากเครื่อง -----
        elif mode == "upload":
            file = request.files.get("file")
            if not file or file.filename == "":
                flash("กรุณาเลือกไฟล์วิดีโอสำหรับอัปโหลด", "error")
                return redirect(url_for("admin_upload"))

            # simple secure filename
            filename = file.filename
            # ป้องกัน path แปลก ๆ
            filename = os.path.basename(filename)
            # เติมเวลาให้ชื่อไฟล์กันชนกัน
            base, ext = os.path.splitext(filename)
            ext = ext.lower() or ".mp4"
            safe_name = f"{base}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(VIDEO_DIR, safe_name)
            file.save(save_path)

            rel_path = os.path.relpath(save_path, os.path.dirname(__file__))
            file_path = rel_path
            source_type = "upload"

        else:
            flash("โหมดที่เลือกไม่ถูกต้อง", "error")
            return redirect(url_for("admin_upload"))

        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO videos (title, description, source_type, video_url,
                                drive_id, file_path, thumbnail_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                description,
                source_type,
                video_url,
                drive_id,
                file_path,
                thumbnail_url,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        flash("เพิ่มวิดีโอสำเร็จแล้ว", "success")
        return redirect(url_for("index"))

    return render_template("upload.html")


# ---------------- Main ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
