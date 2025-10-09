from fastapi import FastAPI, Body, Request
from fastapi.responses import FileResponse, JSONResponse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess, uuid, requests, os, textwrap, json

app = FastAPI(title="Video Reels API")
WORKDIR = Path("/tmp"); WORKDIR.mkdir(exist_ok=True)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
executor = ThreadPoolExecutor(max_workers=1)

# in-memory + disk-backed
JOBS: dict[str, dict] = {}  # job_id -> {"status": "...", "out": Path, "error": str|None, "stderr": str|None}

# ---------- small disk persistence ----------
def _job_json_path(job_id: str) -> Path:
    return WORKDIR / f"{job_id}.json"

def _save_job(job_id: str, data: dict):
    # stringify Path for JSON
    data_to_save = data.copy()
    if isinstance(data_to_save.get("out"), Path):
        data_to_save["out"] = str(data_to_save["out"])
    _job_json_path(job_id).write_text(json.dumps(data_to_save), encoding="utf-8")

def _load_job(job_id: str) -> dict | None:
    p = _job_json_path(job_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if "out" in data and isinstance(data["out"], str):
            data["out"] = Path(data["out"])
        return data
    except Exception:
        return None

# ---------- helpers ----------
def _download(url: str, suffix: str) -> Path:
    p = WORKDIR / f"{uuid.uuid4()}{suffix}"
    r = requests.get(url, stream=True, timeout=(10, 600))
    r.raise_for_status()
    with open(p, "wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                f.write(chunk)
    return p

def _wrap_text(text: str, canvas_w: int = 1080, fontsize: int = 72, side_pad: int = 80) -> str:
    """Грубый перенос по ширине, устойчивый к кириллице/кавычкам."""
    if not text:
        return ""
    avg = 0.6 * fontsize  # средняя ширина глифа
    max_chars = max(8, int((canvas_w - 2 * side_pad) / avg))
    return textwrap.fill(text.strip(), width=max_chars)

def _ffprobe_streams(path: Path) -> dict:
    """Разбор потоков файла: есть ли audio/video, индекс аудио."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).stdout.decode("utf-8", "ignore")
        data = json.loads(out)
        has_video = False
        has_audio = False
        audio_index = None
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                has_video = True
            if s.get("codec_type") == "audio":
                has_audio = True
                if audio_index is None:
                    audio_index = s.get("index", 0)
        return {"has_video": has_video, "has_audio": has_audio, "audio_index": audio_index}
    except Exception:
        return {"has_video": False, "has_audio": False, "audio_index": None}

def _ensure_audio_only(in_path: Path | None) -> Path | None:
    """Вернёт путь к чистому аудио (m4a). Если вход — видео с аудио, вырежет дорожку; если аудио нет — None."""
    if not in_path:
        return None
    info = _ffprobe_streams(in_path)
    if not info["has_audio"]:
        return None
    if info["has_audio"] and not info["has_video"]:
        return in_path  # уже чистое аудио
    # есть видео — вырежем аудио-дорожку
    out_audio = WORKDIR / f"{uuid.uuid4()}_audio.m4a"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(in_path), "-vn", "-acodec", "aac", "-b:a", "192k", str(out_audio)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return out_audio
    except subprocess.CalledProcessError:
        return None

# ---------- core ----------
def _process(video_url: str, music_url: str | None, hook_text: str, out_path: Path, job_id: str):
    in_video = _download(video_url, "_in.mp4")
    music_file = _download(music_url, "_in_audio") if music_url else None
    audio_only = _ensure_audio_only(music_file) if music_file else None

    CANVAS_W, CANVAS_H = 1080, 1920
    FONT_SIZE = 72
    MARGIN = 24

    # пишем текст в файл (надёжно для кавычек/эмодзи)
    wrapped = _wrap_text(hook_text, canvas_w=CANVAS_W, fontsize=FONT_SIZE, side_pad=80)
    textfile = WORKDIR / f"{uuid.uuid4()}_hook.txt"
    textfile.write_text(wrapped, encoding="utf-8")

    # позиция хука: прижать к верхней кромке видео (горизонтальное 16:9)
    # высота видео ~ w*0.5625, верх видео ~ h - (высота_видео)/2
    HOOK_Y_EXPR = f"max(40, h - (w*0.5625)/2 - text_h - {MARGIN})"

    vf = (
        f"scale={CANVAS_W}:-2:force_original_aspect_ratio=decrease,"
        f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=fontfile={FONT_BOLD}:textfile='{textfile}':"
        f"fontcolor=white:fontsize={FONT_SIZE}:line_spacing=8:"
        "box=1:boxcolor=black@0.5:boxborderw=16:"
        "x=(w-text_w)/2:"
        f"y='{HOOK_Y_EXPR}'"
    )

    cmd = ["ffmpeg", "-y", "-i", str(in_video)]
    if audio_only:
        # зациклим музыку и замапим её
        cmd += ["-stream_loop", "-1", "-i", str(audio_only), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        # без музыки: отключаем любой входной звук
        cmd += ["-an"]

    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-shortest", str(out_path)]

    try:
        proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        JOBS[job_id]["stderr"] = proc.stderr.decode("utf-8", "ignore")[-2000:] or None
    except subprocess.CalledProcessError as e:
        JOBS[job_id]["stderr"] = (e.stderr or b"").decode("utf-8", "ignore")[-4000:]
        raise RuntimeError(JOBS[job_id]["stderr"])
    finally:
        # чистим времянку (результат оставляем)
        for p in (in_video, music_file, textfile):
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass
        if audio_only and music_file and audio_only != music_file:
            try: os.remove(audio_only)
            except: pass

# ---------- API ----------
@app.get("/")
def root():
    return {"status": "ok", "message": "Video Reels API is running"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/process_links_async")
def process_links_async(payload: dict = Body(...)):
    video_url = payload.get("video_url")
    music_url = payload.get("music_url")
    hook_text = payload.get("text", "Ваш заголовок")
    if not video_url:
        return JSONResponse({"error": "video_url is required"}, status_code=400)

    job_id = uuid.uuid4().hex
    out_path = WORKDIR / f"{job_id}.mp4"
    JOBS[job_id] = {"status": "queued", "out": out_path, "error": None, "stderr": None}
    _save_job(job_id, JOBS[job_id])

    def run():
        try:
            JOBS[job_id]["status"] = "running"; _save_job(job_id, JOBS[job_id])
            _process(video_url, music_url, hook_text, out_path, job_id)
            JOBS[job_id]["status"] = "done"; _save_job(job_id, JOBS[job_id])
        except Exception as e:
            JOBS[job_id]["status"] = "error"; JOBS[job_id]["error"] = str(e); _save_job(job_id, JOBS[job_id])

    executor.submit(run)
    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/status/{job_id}",
        "result_url": f"/result/{job_id}",
    }

@app.get("/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id) or _load_job(job_id)
    if not job:
        # если job.json нет, но результат есть — считаем done
        out_path = WORKDIR / f"{job_id}.mp4"
        if out_path.exists():
            return {"job_id": job_id, "status": "done", "error": None}
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"job_id": job_id, "status": job["status"], "error": job.get("error"), "stderr_tail": job.get("stderr")}

# /result -> отдаём ссылку для скачивания (fallback по файлу)
@app.get("/result/{job_id}")
def result(job_id: str, request: Request):
    base_url = str(request.base_url).rstrip("/")
    out_path = WORKDIR / f"{job_id}.mp4"
    if out_path.exists():
        return {"download_url": f"{base_url}/download/{job_id}"}

    job = JOBS.get(job_id) or _load_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job["status"] != "done":
        return JSONResponse({"error": "not ready", "status": job["status"]}, status_code=425)
    return {"download_url": f"{base_url}/download/{job_id}"}

@app.get("/download/{job_id}")
def download(job_id: str):
    # 1) память
    job = JOBS.get(job_id)
    if job and Path(job["out"]).exists():
        return FileResponse(str(job["out"]), filename=f"processed_{job_id}.mp4")
    # 2) файл напрямую (другой инстанс/рестарт)
    out_path = WORKDIR / f"{job_id}.mp4"
    if out_path.exists():
        return FileResponse(str(out_path), filename=f"processed_{job_id}.mp4")
    return JSONResponse({"error": "not found"}, status_code=404)
