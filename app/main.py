from fastapi import FastAPI, UploadFile, Form, Body
from fastapi.responses import FileResponse, JSONResponse
import subprocess, uuid, shutil, os, requests
from pathlib import Path

app = FastAPI(title="Video Reels API")

WORKDIR = Path("/tmp")
WORKDIR.mkdir(exist_ok=True)

@app.get("/")
def root():
    return {"ok": True, "service": "video-reels-api", "endpoints": ["/health", "/process"]}

@app.get("/health")
def health():
    return {"status": "ok"}

# Вариант 1: multipart (если будет удобно когда-нибудь грузить файл)
@app.post("/process")
async def process_form(file: UploadFile, text: str = Form("Мой текст")):
    input_path = WORKDIR / f"{uuid.uuid4()}_{file.filename}"
    output_path = WORKDIR / f"{uuid.uuid4()}_out.mp4"

    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    vf = (
        "scale=-1:1080,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=text='{text.replace(':', r'\\:').replace(\"'\", r\"\\'\")}':"
        "fontcolor=white:fontsize=64:x=(w-text_w)/2:y=80"
    )

    cmd = ["ffmpeg","-y","-i",str(input_path), "-an",
           "-vf", vf,
           "-c:v","libx264","-preset","veryfast","-crf","23",
           str(output_path)]
    subprocess.run(cmd, check=True)

    return FileResponse(output_path, filename="processed.mp4")

# Вариант 2: JSON с ссылками (твой случай: видео/музыка по URL + текст)
@app.post("/process_links")
def process_links(payload: dict = Body(...)):
    video_url = payload.get("video_url")
    music_url = payload.get("music_url")
    text = payload.get("text", "Мой текст")

    if not video_url:
        return JSONResponse({"error": "video_url is required"}, status_code=400)

    in_video = WORKDIR / f"{uuid.uuid4()}_in.mp4"
    in_music = WORKDIR / f"{uuid.uuid4()}_in.mp3"
    out_path = WORKDIR / f"{uuid.uuid4()}_out.mp4"

    # Скачиваем видео
    r = requests.get(video_url, stream=True, timeout=120)
    r.raise_for_status()
    with open(in_video, "wb") as f:
        for chunk in r.iter_content(chunk_size=1048576):
            if chunk: f.write(chunk)

    vf = (
        "scale=-1:1080,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=text='{text.replace(':', r'\\:').replace(\"'\", r\"\\'\")}':"
        "fontcolor=white:fontsize=64:x=(w-text_w)/2:y=80"
    )

    if music_url:
        # Скачиваем музыку
        r2 = requests.get(music_url, stream=True, timeout=120)
        r2.raise_for_status()
        with open(in_music, "wb") as f:
            for chunk in r2.iter_content(chunk_size=1048576):
                if chunk: f.write(chunk)

        cmd = ["ffmpeg","-y","-i",str(in_video),"-i",str(in_music),
               "-map","0:v","-map","1:a","-shortest",
               "-vf", vf,
               "-c:v","libx264","-preset","veryfast","-crf","23",
               "-c:a","aac","-b:a","128k",
               str(out_path)]
    else:
        cmd = ["ffmpeg","-y","-i",str(in_video), "-an",
               "-vf", vf,
               "-c:v","libx264","-preset","veryfast","-crf","23",
               str(out_path)]

    subprocess.run(cmd, check=True)
    return FileResponse(out_path, filename="processed.mp4")
