"""AUTOCLIP - backend Flask.

Alur kerja:
    URL YouTube -> validasi -> unduh (yt-dlp) -> potong (FFmpeg) -> unduh klip
"""

import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import yt_dlp
from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from yt_dlp.utils import DownloadError

# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    """Baca angka dari environment variable; pakai default jika tidak valid."""
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_VIDEO_SECONDS = _env_int("AUTOCLIP_MAX_VIDEO_SECONDS", 1800)  # 30 menit
MAX_CLIPS = max(2, min(_env_int("AUTOCLIP_MAX_CLIPS", 12), 99))
MIN_CLIP_SECONDS = 15
MAX_CLIP_SECONDS = 60
DEFAULT_CLIP_SECONDS = 30
MAX_HEIGHT = 720  # resolusi maksimal video yang diunduh
JOB_TTL_SECONDS = 3600  # hasil dihapus otomatis setelah 1 jam
FFMPEG_TIMEOUT = 600  # detik per klip

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
CLIP_NAME_RE = re.compile(r"^clip_\d{2,3}\.mp4$")
ACTIVE_STATUSES = {"queued", "analyzing", "downloading", "cutting"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # request JSON kecil saja

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=2)


class ClipError(Exception):
    """Error yang pesannya aman ditampilkan langsung ke pengguna."""


# --------------------------------------------------------------------------
# Fungsi bantu
# --------------------------------------------------------------------------


def get_ffmpeg() -> Optional[str]:
    """Kembalikan path FFmpeg, atau None jika belum terpasang."""
    return shutil.which("ffmpeg")


def format_time(seconds: int) -> str:
    """Ubah detik menjadi MM:SS atau H:MM:SS."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def normalize_youtube_url(raw: Any) -> Optional[str]:
    """Validasi URL YouTube dan ubah ke bentuk baku.

    Mengembalikan https://www.youtube.com/watch?v=<id>, atau None jika
    URL bukan link video YouTube yang valid (parameter playlist dibuang).
    """
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > 300:
        return None
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if host not in YOUTUBE_HOSTS:
        return None

    video_id = None
    if host.endswith("youtu.be"):
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [None])[0]
    else:
        match = re.match(r"^/(shorts|embed|live|v)/([^/?#]+)", parsed.path)
        if match:
            video_id = match.group(2)

    if not video_id or not VIDEO_ID_RE.match(video_id):
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def translate_ytdlp_error(message: str) -> str:
    """Terjemahkan pesan error yt-dlp menjadi pesan Bahasa Indonesia."""
    text = message.lower()
    rules = [
        (
            ("private video", "this video is private"),
            "Video ini bersifat private, sehingga tidak bisa diunduh.",
        ),
        (
            ("members-only", "join this channel"),
            "Video ini khusus anggota channel dan tidak bisa diunduh.",
        ),
        (
            ("confirm your age", "age-restricted", "inappropriate for some users"),
            "Video ini dibatasi usia dan tidak bisa diunduh tanpa login.",
        ),
        (
            ("not a bot",),
            "YouTube meminta verifikasi anti-bot dari server ini. "
            "Coba lagi nanti atau perbarui yt-dlp (pip install -U yt-dlp).",
        ),
        (
            ("your country", "blocked it", "copyright"),
            "Video ini diblokir (hak cipta atau pembatasan wilayah).",
        ),
        (
            ("live event", "is live", "premieres in"),
            "Video live atau yang belum tayang tidak bisa diproses.",
        ),
        (
            ("video unavailable", "has been removed", "been terminated",
             "does not exist", "unavailable"),
            "Video tidak tersedia (sudah dihapus atau tidak dapat diakses).",
        ),
        (
            ("unable to download webpage", "urlopen error", "timed out",
             "temporary failure", "network is unreachable"),
            "Koneksi ke YouTube gagal. Periksa internet server lalu coba lagi.",
        ),
    ]
    for keywords, answer in rules:
        if any(word in text for word in keywords):
            return answer
    return "Gagal mengambil video dari YouTube. Pastikan link benar dan video bisa diputar."


def plan_clips(duration: int, clip_len: int) -> List[Tuple[int, int]]:
    """Rencanakan potongan (mulai, durasi) dalam detik.

    Potongan berurutan sepanjang clip_len. Sisa di akhir yang lebih pendek
    dari MIN_CLIP_SECONDS dibuang. Jika jumlah klip melebihi MAX_CLIPS,
    klip dipilih merata di sepanjang video.
    """
    starts: List[int] = []
    t = 0
    while t < duration:
        if duration - t < MIN_CLIP_SECONDS and starts:
            break
        starts.append(t)
        t += clip_len

    if len(starts) > MAX_CLIPS:
        step = (len(starts) - 1) / (MAX_CLIPS - 1)
        starts = [starts[round(i * step)] for i in range(MAX_CLIPS)]

    return [(s, min(clip_len, duration - s)) for s in starts]


# --------------------------------------------------------------------------
# Manajemen job
# --------------------------------------------------------------------------


def set_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def cleanup_old_jobs() -> None:
    """Hapus job dan folder hasil yang sudah melewati batas waktu."""
    now = time.time()
    with JOBS_LOCK:
        expired = [
            jid
            for jid, job in JOBS.items()
            if job["status"] not in ACTIVE_STATUSES
            and now - job["created_at"] > JOB_TTL_SECONDS
        ]
        for jid in expired:
            JOBS.pop(jid, None)
        known = set(JOBS)

    for jid in expired:
        shutil.rmtree(DOWNLOAD_DIR / jid, ignore_errors=True)

    # Folder sisa dari proses server sebelumnya
    for entry in DOWNLOAD_DIR.iterdir():
        if entry.is_dir() and JOB_ID_RE.match(entry.name) and entry.name not in known:
            try:
                too_old = now - entry.stat().st_mtime > JOB_TTL_SECONDS
            except OSError:
                continue
            if too_old:
                shutil.rmtree(entry, ignore_errors=True)


# --------------------------------------------------------------------------
# Pemrosesan video
# --------------------------------------------------------------------------


def fetch_info(url: str) -> Dict[str, Any]:
    """Ambil metadata video dan cek ketersediaan serta batas durasi."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise ClipError(translate_ytdlp_error(str(exc)))
    except Exception:
        raise ClipError("Gagal mengambil informasi video dari YouTube.")

    if not info:
        raise ClipError("Informasi video tidak ditemukan.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise ClipError("Video live atau yang belum tayang tidak bisa diproses.")

    duration = info.get("duration")
    if not duration:
        raise ClipError("Durasi video tidak diketahui, sehingga tidak bisa dipotong.")
    if duration > MAX_VIDEO_SECONDS:
        raise ClipError(
            f"Durasi video ({format_time(duration)}) melebihi batas "
            f"{format_time(MAX_VIDEO_SECONDS)}. Pilih video yang lebih pendek."
        )
    return info


def download_video(job_id: str, url: str, job_dir: Path) -> Path:
    """Unduh video ke job_dir/source.<ext> dan kembalikan path-nya."""

    def progress_hook(data: Dict[str, Any]) -> None:
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if total:
            percent = data.get("downloaded_bytes", 0) / total * 100
            set_job(
                job_id,
                progress=int(5 + percent * 0.55),
                message=f"Mengunduh video... {percent:.0f}%",
            )

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "progress_hooks": [progress_hook],
        "socket_timeout": 30,
        "retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except DownloadError as exc:
        raise ClipError(translate_ytdlp_error(str(exc)))
    except Exception:
        raise ClipError("Gagal mengunduh video dari YouTube.")

    candidates = [
        p
        for p in job_dir.glob("source.*")
        if p.is_file() and p.suffix not in (".part", ".ytdl")
    ]
    if not candidates:
        raise ClipError("File video hasil unduhan tidak ditemukan.")
    return max(candidates, key=lambda p: p.stat().st_size)


def cut_clip(source: Path, output: Path, start: int, length: int) -> None:
    """Potong satu klip dengan FFmpeg (re-encode agar potongan akurat)."""
    ffmpeg = get_ffmpeg()
    if not ffmpeg:
        raise ClipError("FFmpeg tidak terpasang di server.")
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start), "-i", str(source), "-t", str(length),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise ClipError("Pemotongan video terlalu lama dan dibatalkan.")
    except OSError:
        raise ClipError("FFmpeg tidak dapat dijalankan di server.")
    if result.returncode != 0 or not output.is_file():
        app.logger.error("FFmpeg gagal: %s", result.stderr.strip()[-500:])
        raise ClipError("FFmpeg gagal memotong video.")


def process_job(job_id: str, url: str, clip_len: int) -> None:
    """Pekerjaan latar belakang: analisis, unduh, lalu potong video."""
    job_dir = DOWNLOAD_DIR / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        set_job(job_id, status="analyzing", progress=3, message="Memeriksa video...")
        info = fetch_info(url)
        duration = int(info["duration"])
        set_job(
            job_id,
            title=info.get("title") or "Video YouTube",
            video_duration=duration,
            status="downloading",
            progress=5,
            message="Mengunduh video...",
        )

        source = download_video(job_id, url, job_dir)

        segments = plan_clips(duration, clip_len)
        total = len(segments)
        clips: List[Dict[str, Any]] = []
        for index, (start, length) in enumerate(segments, start=1):
            name = f"clip_{index:02d}.mp4"
            set_job(
                job_id,
                status="cutting",
                progress=int(60 + (index - 1) / total * 40),
                message=f"Memotong klip {index} dari {total}...",
            )
            cut_clip(source, job_dir / name, start, length)
            clips.append(
                {
                    "name": name,
                    "index": index,
                    "start": start,
                    "end": start + length,
                    "duration": length,
                    "start_label": format_time(start),
                    "end_label": format_time(start + length),
                }
            )

        try:
            source.unlink()  # hemat disk: video sumber tidak dibutuhkan lagi
        except OSError:
            pass

        set_job(
            job_id,
            status="done",
            progress=100,
            message=f"Selesai! {total} klip siap diunduh.",
            clips=clips,
        )
    except ClipError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        set_job(job_id, status="error", error=str(exc), message=str(exc))
    except Exception:
        app.logger.exception("Error tak terduga pada job %s", job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        msg = "Terjadi kesalahan tak terduga di server."
        set_job(job_id, status="error", error=msg, message=msg)


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return render_template(
        "index.html",
        ffmpeg_ok=get_ffmpeg() is not None,
        min_clip=MIN_CLIP_SECONDS,
        max_clip=MAX_CLIP_SECONDS,
        default_clip=DEFAULT_CLIP_SECONDS,
        max_video_minutes=MAX_VIDEO_SECONDS // 60,
        max_clips=MAX_CLIPS,
    )


@app.get("/api/health")
def health():
    return jsonify(
        ffmpeg=get_ffmpeg() is not None,
        yt_dlp_version=yt_dlp.version.__version__,
        max_video_seconds=MAX_VIDEO_SECONDS,
    )


@app.post("/api/process")
def start_process():
    data = request.get_json(silent=True) or {}

    url = normalize_youtube_url(data.get("url"))
    if not url:
        return jsonify(error="URL tidak valid. Masukkan link video YouTube yang benar."), 400

    try:
        clip_len = int(data.get("clip_seconds", DEFAULT_CLIP_SECONDS))
    except (TypeError, ValueError):
        return jsonify(error="Durasi klip tidak valid."), 400
    if not MIN_CLIP_SECONDS <= clip_len <= MAX_CLIP_SECONDS:
        return (
            jsonify(error=f"Durasi klip harus antara {MIN_CLIP_SECONDS} dan {MAX_CLIP_SECONDS} detik."),
            400,
        )

    if not get_ffmpeg():
        return (
            jsonify(error="FFmpeg belum terpasang di server. Lihat README.md bagian prasyarat."),
            500,
        )

    cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "Menunggu antrean...",
            "title": "",
            "clips": [],
            "error": None,
            "created_at": time.time(),
        }
    EXECUTOR.submit(process_job, job_id, url, clip_len)
    return jsonify(job_id=job_id), 202


@app.get("/api/status/<job_id>")
def job_status(job_id: str):
    if not JOB_ID_RE.match(job_id):
        abort(404)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        snapshot = dict(job) if job else None
    if snapshot is None:
        return jsonify(error="Job tidak ditemukan atau sudah kedaluwarsa."), 404

    clips = []
    for clip in snapshot["clips"]:
        clips.append(
            {
                **clip,
                "preview_url": url_for("serve_clip", job_id=job_id, filename=clip["name"]),
                "download_url": url_for("serve_clip", job_id=job_id, filename=clip["name"], dl=1),
            }
        )
    snapshot["clips"] = clips
    snapshot.pop("created_at", None)
    return jsonify(snapshot)


@app.get("/files/<job_id>/<filename>")
def serve_clip(job_id: str, filename: str):
    if not JOB_ID_RE.match(job_id) or not CLIP_NAME_RE.match(filename):
        abort(404)
    directory = DOWNLOAD_DIR / job_id
    if not (directory / filename).is_file():
        abort(404)
    as_attachment = request.args.get("dl") == "1"
    return send_from_directory(
        directory,
        filename,
        as_attachment=as_attachment,
        download_name=f"autoclip_{job_id[:8]}_{filename}",
        mimetype="video/mp4",
    )


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=_env_int("PORT", 5000),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
