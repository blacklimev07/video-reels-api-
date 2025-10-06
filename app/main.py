from fastapi import FastAPI, Body
from fastapi.responses import FileResponse, JSONResponse
from concurrent.futures import ThreadPoolExecutor
import subprocess, uuid, requests, os
from pathlib import Path

app = FastAPI(title="Video Reels API")
WORKDIR = Path("/tmp"); WORKDIR.mkdir(exist_ok=True)

executor = ThreadPoolExecutor(max_workers=1)
JOBS: dict[str, dict] = {}  # job_id -> {"status": "...", "out": Path, "error": str|None}


def _process(video_url: str, music_url: str | None, text: str, out_path: Path):
    # скачиваем видео
    in_video = WORKDIR / f"{uuid.uuid4()}_in.mp4"
    r = requests.get(video_url, stream=True, timeout=(10, 600))
    r.raise_for_status()
    with open(in_video, "wb") as f:
        for chunk in r.iter_content(1024*1024):
            if chunk: f.write(chunk)

    in_audio = None
    if music_url:
        in_audio = WORKDIR / f"{uuid.uuid4()}_in_audio"
        r = requests.get(music_url, stream=True, timeout=(10, 600))
        r.raise_for_status()
        with open(in_audio, "wb") as f:
            for chunk in r.iter_content(1024*1024):
                if chunk: f.write(chunk)

    # параметры канвы
    CANVAS_W, CANVAS_H = 720, 1280
    FONT_SIZE = 56
    FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # текст с экранированием
    safe_text = text.replace(":", r"\:").replace("'", r"\'")

    # формула позиции: чуть выше видео
    HOOK_Y_EXPR = "((oh-ih)/2)-(text_h+24)"

    # собираем фильтры
    vf = (
        f"scale={CANVAS_W}:-2:force_original_aspect_ratio=decrease,"
        f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=fontfile={FONT_BOLD}:text='{safe_text}':"
        f"fontcolor=white:fontsize={FONT_SIZE}:line_spacing=8:"
        "box=1:boxcolor=black@0.5:boxborderw=16:"
        "x=(w-text_w)/2:"
        f"y={HOOK_Y_EXPR}"
    )

    cmd = [
        "ffmpeg","-y","-i",str(in_video),
    ]
    if in_audio:
        cmd += ["-i", str(in_audio), "-map", "0:v", "-map", "1:a", "-shortest"]
    else:
        cmd += ["-an"]

    cmd += ["-vf", vf, "-c:v","libx264","-preset","ultrafast","-crf","25", str(out_path)]
    subprocess.run(cmd, check=True)


@app.get("/health")
def health(): 
    return {"status":"ok"}


@app.post("/process_links_async")
def process_links_async(payload: dict = Body(...)):
    video_url = payload.get("video_url")
    music_url = payload.get("music_url")
    text = payload.get("text","Мой текст")
    if not video_url:
        return JSONResponse({"error":"video_url is required"}, status_code=400)

    job_id = uuid.uuid4().hex
    out_path = WORKDIR / f"{job_id}.mp4"
    JOBS[job_id] = {"status":"queued", "out": out_path, "error": None}

    def run():
        try:
            JOBS[job_id]["status"] = "running"
            _process(video_url, music_url, text, out_path)
            JOBS[job_id]["status"] = "done"
        except Exception as e:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)

    executor.submit(run)
    return {
        "job_id": job_id,
        "status": "queued",
        "result_url": f"/result/{job_id}",
        "status_url": f"/status/{job_id}"
    }


@app.get("/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job: return JSONResponse({"error":"not found"}, status_code=404)
    return {"job_id": job_id, "status": job["status"], "error": job["error"]}


@app.get("/result/{job_id}")
def result(job_id: str):
    job = JOBS.get(job_id)
    if not job: 
        return JSONResponse({"error":"not found"}, status_code=404)
    if job["status"] != "done": 
        return JSONResponse({"error":"not ready", "status": job["status"]}, status_code=425)

    # отдаём готовый mp4
    return FileResponse(str(job["out"]), filename=f"processed_{job_id}.mp4", media_type="video/mp4")
