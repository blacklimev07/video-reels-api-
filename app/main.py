from fastapi import FastAPI, Body, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import RedirectResponse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess, uuid, requests, os, shutil

app = FastAPI(title="Video Reels API")
WORKDIR = Path("/tmp"); WORKDIR.mkdir(exist_ok=True)

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
executor = ThreadPoolExecutor(max_workers=1)
JOBS: dict[str, dict] = {}  # job_id -> {"status": "...", "out": Path, "error": str|None, "stderr": str|None}

def _download(url: str, suffix: str) -> Path:
    p = WORKDIR / f"{uuid.uuid4()}{suffix}"
    r = requests.get(url, stream=True, timeout=(10, 600))
    r.raise_for_status()
    with open(p, "wb") as f:
        for chunk in r.iter_content(1024*1024):
            if chunk: f.write(chunk)
    return p

def _escape_drawtext(txt: str) -> str:
    return txt.replace("\\", r"\\\\").replace(":", r"\:").replace("'", r"\'").replace("\n", r"\n")

def _process(video_url: str, music_url: str | None, text: str, out_path: Path, job_id: str):
    in_video = _download(video_url, "_in.mp4")
    in_audio = _download(music_url, "_in_audio") if music_url else None

    safe_text = _escape_drawtext(text or "")
    vf = (
        "scale=720:1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=fontfile={FONT_PATH}:text='{safe_text}':"
        "fontcolor=white:fontsize=56:box=1:boxcolor=black@0.5:boxborderw=12:x=(w-text_w)/2:y=80"
    )

    cmd = ["ffmpeg","-y","-i",str(in_video)]
    if in_audio:
        cmd += ["-stream_loop","-1","-i",str(in_audio), "-map","0:v:0","-map","1:a:0","-c:a","aac","-b:a","192k"]
    else:
        cmd += ["-an"]

    cmd += ["-vf", vf, "-c:v","libx264","-preset","ultrafast","-crf","23", "-shortest", str(out_path)]

    try:
        proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        JOBS[job_id]["stderr"] = proc.stderr.decode("utf-8", "ignore")[-2000:] or None
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "ignore")
        JOBS[job_id]["stderr"] = err[-4000:]
        raise RuntimeError(f"ffmpeg failed: {JOBS[job_id]['stderr']}")
    finally:
        for p in (in_video, in_audio):
            if p and os.path.exists(p): 
                try: os.remove(p)
                except: pass

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
    JOBS[job_id] = {"status":"queued", "out": out_path, "error": None, "stderr": None}

    def run():
        try:
            JOBS[job_id]["status"] = "running"
            _process(video_url, music_url, text, out_path, job_id)
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

# теперь result не отдает файл, а возвращает ссылку download_url
@app.get("/result/{job_id}")
def result(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if not job: return JSONResponse({"error":"not found"}, status_code=404)
    if job["status"] != "done": return JSONResponse({"error":"not ready", "status": job["status"]}, status_code=425)
    base_url = str(request.base_url).rstrip("/")
    download_url = f"{base_url}/download/{job_id}"
    return {"download_url": download_url}

@app.get("/download/{job_id}")
def download(job_id: str):
    job = JOBS.get(job_id)
    if not job: return JSONResponse({"error":"not found"}, status_code=404)
    if job["status"] != "done": return JSONResponse({"error":"not ready"}, status_code=425)
    return FileResponse(str(job["out"]), filename=f"processed_{job_id}.mp4")
