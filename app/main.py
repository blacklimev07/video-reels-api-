from fastapi import FastAPI, Body, Request
from fastapi.responses import FileResponse, JSONResponse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess, uuid, requests, os, textwrap

app = FastAPI(title="Video Reels API")
WORKDIR = Path("/tmp"); WORKDIR.mkdir(exist_ok=True)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
executor = ThreadPoolExecutor(max_workers=1)
JOBS: dict[str, dict] = {}  # job_id -> {"status": "...", "out": Path, "error": str|None, "stderr": str|None}


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
    """Грубый устойчивый перенос по ширине канвы."""
    if not text:
        return ""
    avg = 0.6 * fontsize  # средняя ширина глифа
    max_chars = max(8, int((canvas_w - 2 * side_pad) / avg))
    return textwrap.fill(text.strip(), width=max_chars)


# ---------- core ----------
def _process(video_url: str, music_url: str | None, hook_text: str, out_path: Path, job_id: str):
    in_video = _download(video_url, "_in.mp4")
    in_audio = _download(music_url, "_in_audio") if music_url else None

    CANVAS_W, CANVAS_H = 1080, 1920
    FONT_SIZE = 72
    MARGIN = 24

    # пишем текст в файл (надёжно для кавычек/эмодзи)
    wrapped = _wrap_text(hook_text, canvas_w=CANVAS_W, fontsize=FONT_SIZE, side_pad=80)
    textfile = WORKDIR / f"{uuid.uuid4()}_hook.txt"
    textfile.write_text(wrapped, encoding="utf-8")

    # позиция хука: прижать к верхней кромке видео (предполагаем горизонтальное 16:9)
    # высота видео ~ w*0.5625, верх видео ~ (h - w*0.5625)/2
    # финально: y = max(40, верх - text_h - MARGIN)
    HOOK_Y_EXPR = f"max(40,(h-(w*0.5625))/2-text_h-{MARGIN})"

    vf = (
        f"scale={CANVAS_W}:-2:force_original_aspect_ratio=decrease,"
        f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=fontfile={FONT_BOLD}:textfile='{textfile}':"
        f"fontcolor=white:fontsize={FONT_SIZE}:line_spacing=8:"
        "box=1:boxcolor=black@0.5:boxborderw=16:"
        "x=(w-text_w)/2:"
        f"y={HOOK_Y_EXPR}"
    )

    cmd = ["ffmpeg", "-y", "-i", str(in_video)]
    if in_audio:
        # зацикливаем музыку, чтобы хватило на длину видео
        cmd += ["-stream_loop", "-1", "-i", str(in_audio), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]

    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-shortest", str(out_path)]

    try:
        proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        JOBS[job_id]["stderr"] = proc.stderr.decode("utf-8", "ignore")[-2000:] or None
    except subprocess.CalledProcessError as e:
        JOBS[job_id]["stderr"] = (e.stderr or b"").decode("utf-8", "ignore")[-4000:]
        raise RuntimeError(JOBS[job_id]["stderr"])
    finally:
        for p in (in_video, in_audio, textfile):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except:
                    pass


# ---------- API ----------
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

    def run():
        try:
            JOBS[job_id]["status"] = "running"
            _process(video_url, music_url, hook_text, out_path, job_id)
            JOBS[job_id]["status"] = "done"
        except Exception as e:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)

    executor.submit(run)
    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/status/{job_id}",
        "result_url": f"/result/{job_id}",
    }


@app.get("/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"job_id": job_id, "status": job["status"], "error": job["error"], "stderr_tail": job["stderr"]}


# /result -> отдаём ссылку для скачивания
@app.get("/result/{job_id}")
def result(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job["status"] != "done":
        return JSONResponse({"error": "not ready", "status": job["status"]}, status_code=425)
    base_url = str(request.base_url).rstrip("/")
    download_url = f"{base_url}/download/{job_id}"
    return {"download_url": download_url}


@app.get("/download/{job_id}")
def download(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job["status"] != "done":
        return JSONResponse({"error": "not ready"}, status_code=425)
    return FileResponse(str(job["out"]), filename=f"processed_{job_id}.mp4")
