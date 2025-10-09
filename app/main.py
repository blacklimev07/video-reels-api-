from fastapi import FastAPI, Body, Request
from fastapi.responses import FileResponse, JSONResponse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess, uuid, requests, os, textwrap, json

app = FastAPI(title="Montager API")
WORKDIR = Path("/tmp"); WORKDIR.mkdir(exist_ok=True)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
executor = ThreadPoolExecutor(max_workers=1)
JOBS: dict[str, dict] = {}

# ---------------- Utils ----------------

def _download(url: str, suffix: str) -> Path:
    p = WORKDIR / f"{uuid.uuid4()}{suffix}"
    r = requests.get(url, stream=True, timeout=(10, 600))
    r.raise_for_status()
    with open(p, "wb") as f:
        for chunk in r.iter_content(1024*1024):
            if chunk:
                f.write(chunk)
    return p

def _wrap_text(text: str, canvas_w: int = 1080, fontsize: int = 72, side_pad: int = 80) -> str:
    if not text:
        return ""
    avg = 0.6 * fontsize
    max_chars = max(8, int((canvas_w - 2*side_pad) / avg))
    return textwrap.fill(text.strip(), width=max_chars)

def _ffprobe_streams(path: Path) -> dict:
    """Вернёт словарь с флагами has_video/has_audio и индексом аудио-потока (если есть)."""
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

def _ensure_audio_only(in_path: Path) -> Path | None:
    """
    Гарантирует, что вернётся путь на чистое аудио (aac/m4a). Если аудио не найдено — вернёт None.
    - Если вход уже аудио → просто возвращаем его.
    - Если вход mp4 с аудио → вырезаем аудиодорожку.
    - Если аудио нет → None.
    """
    info = _ffprobe_streams(in_path)
    if not info["has_audio"]:
        return None

    # Если файл и так без видео — вернём как есть
    if info["has_audio"] and not info["has_video"]:
        return in_path

    # Иначе вытащим аудио-дорожку
    out_audio = WORKDIR / f"{uuid.uuid4()}_audio.m4a"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(in_path), "-vn", "-acodec", "aac", "-b:a", "192k", str(out_audio)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return out_audio
    except subprocess.CalledProcessError:
        return None

# ---------------- Core ----------------

def _process(video_url: str, music_url: str | None, hook_text: str, out_path: Path, job_id: str):
    in_video = _download(video_url, "_in.mp4")
    music_path = _download(music_url, "_in_audio") if music_url else None

    CANVAS_W, CANVAS_H = 1080, 1920
    FONT_SIZE = 72
    MARGIN = 24

    # Текст → переносы → в файл
    wrapped = _wrap_text(hook_text, canvas_w=CANVAS_W, fontsize=FONT_SIZE, side_pad=80)
    textfile = WORKDIR / f"{uuid.uuid4()}_hook.txt"
    textfile.write_text(wrapped, encoding="utf-8")

    # Формула позиционирования (горизонтальное 16:9): y чуть выше видео
    HOOK_Y_EXPR = f"max(40, h - (w*0.5625)/2 - text_h - {MARGIN})"

    # Фильтр
    vf = (
        f"scale={CANVAS_W}:-2:force_original_aspect_ratio=decrease,"
        f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=fontfile={FONT_BOLD}:textfile='{textfile}':"
        f"fontcolor=white:fontsize={FONT_SIZE}:line_spacing=8:"
        "box=1:boxcolor=black@0.5:boxborderw=16:"
        "x=(w-text_w)/2:"
        f"y='{HOOK_Y_EXPR}'"
    )

    # Аудио: делаем устойчиво
    audio_only = _ensure_audio_only(music_path) if music_path else None

    cmd = ["ffmpeg", "-y", "-i", str(in_video)]
    if audio_only:
        # зациклим музыку и замапим её, если есть
        cmd += ["-stream_loop", "-1", "-i", str(audio_only), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        # если музыки нет/не получилось достать аудио — убираем оригинальный звук
        cmd += ["-an"]

    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-shortest", str(out_path)]

    try:
        proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        JOBS[job_id]["stderr"] = proc.stderr.decode("utf-8", "ignore")[-2000:] or None
    except subprocess.CalledProcessError as e:
        JOBS[job_id]["stderr"] = (e.stderr or b"").decode("utf-8", "ignore")[-4000:]
        raise RuntimeError(JOBS[job_id]["stderr"])
    finally:
        # чистим временные файлы (результат оставляем)
        for p in (in_video, music_path, textfile):
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass
        # если создавали вырезанное аудио — его тоже чистим
        if 'audio_only' in locals() and audio_only and audio_only != music_path:
            try: os.remove(audio_only)
            except: pass

# ---------------- API ----------------

@app.get("/")
def root():
    return {"status": "ok", "message": "Montager API is running"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/process_links_async")
def process_links_async(payload: dict = Body(...)):
    video_url = payload.get("video_url")
    music_url = payload.get("music_url")
    hook_text = payload.get("text", "Ваш заголовок")
    if not video_url or not isinstance(video_url, str):
        return JSONResponse({"error": "video_url is required"}, status_code=400)

    job_id = uuid.uuid4().hex
    out_path = WORKDIR / f"{job_id}.mp4"
    JOBS[job_id] = {"status": "queued", "out": out_path, "error": None, "stderr": None}

    def run():
        try:
            JOBS[job_id]["status"] = "running"
            _process(video_url, music_url, hook_text, out_path, job_id)
            JOBS[job_id]["status"] = "done"
        except Exception as e:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)

    executor.submit(run)
    return {"job_id": job_id, "status": "queued",
            "status_url": f"/status/{job_id}",
            "result_url": f"/result/{job_id}"}

@app.get("/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job: return JSONResponse({"error":"not found"}, status_code=404)
    return {"job_id": job_id, "status": job["status"], "error": job["error"], "stderr_tail": job["stderr"]}

@app.get("/result/{job_id}")
def result(job_id: str, request: Request):
    # сначала проверим файл на диске — чтобы не зависеть от памяти/инстанса
    base_url = str(request.base_url).rstrip("/")
    out_path = WORKDIR / f"{job_id}.mp4"
    if out_path.exists():
        return {"download_url": f"{base_url}/download/{job_id}"}

    # иначе смотрим память
    job = JOBS.get(job_id)
    if job and job["status"] == "done" and job["out"].exists():
        return {"download_url": f"{base_url}/download/{job_id}"}

    status_val = (job or {}).get("status", "processing_or_missing")
    return JSONResponse({"error": "not ready", "status": status_val}, status_code=425)

@app.get("/download/{job_id}")
def download(job_id: str):
    # пробуем через память
    job = JOBS.get(job_id)
    if job and job["out"].exists():
        return FileResponse(str(job["out"]), filename=f"processed_{job_id}.mp4")

    # пробуем напрямую по файлу (другой инстанс/рестарт)
    out_path = WORKDIR / f"{job_id}.mp4"
    if out_path.exists():
        return FileResponse(str(out_path), filename=f"processed_{job_id}.mp4")

    return JSONResponse({"error": "not found"}, status_code=404)
