from fastapi import FastAPI, Body
from fastapi.responses import FileResponse, JSONResponse
import subprocess, uuid, requests
from pathlib import Path

app = FastAPI(title="Video Reels API")
WORKDIR = Path("/tmp"); WORKDIR.mkdir(exist_ok=True)

@app.get("/")
def root():
    return {"ok": True, "endpoints": ["/health", "/process_links"]}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/process_links")
def process_links(payload: dict = Body(...)):
    video_url = payload.get("video_url")
    music_url = payload.get("music_url")
    text = payload.get("text", "Мой текст")
    if not video_url:
        return JSONResponse({"error": "video_url is required"}, status_code=400)

    in_video = WORKDIR / f"{uuid.uuid4()}_in.mp4"
    out_path = WORKDIR / f"{uuid.uuid4()}_out.mp4"

    # скачать видео
    r = requests.get(video_url, stream=True, timeout=120); r.raise_for_status()
    with open(in_video, "wb") as f:
        for chunk in r.iter_content(1024*1024):
            if chunk: f.write(chunk)

    # фильтр: 9:16, по центру, белый текст, без оригинального звука
    safe_text = text.replace(":", r"\:").replace("'", r"\'")
    vf = (
        "scale=-1:1080,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=text='{safe_text}':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=80"
    )

    cmd = ["ffmpeg","-y","-i",str(in_video),"-an","-vf",vf,
           "-c:v","libx264","-preset","veryfast","-crf","23", str(out_path)]
    subprocess.run(cmd, check=True)
    return FileResponse(out_path, filename="processed.mp4")
