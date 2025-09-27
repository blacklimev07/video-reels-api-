from fastapi import FastAPI, UploadFile, Form
from fastapi.responses import FileResponse
import subprocess, uuid, shutil
from pathlib import Path

app = FastAPI()

WORKDIR = Path("/tmp")
WORKDIR.mkdir(exist_ok=True)

@app.post("/process")
async def process_video(file: UploadFile, text: str = Form("Hello")):
    input_path = WORKDIR / f"{uuid.uuid4()}_{file.filename}"
    output_path = WORKDIR / f"{uuid.uuid4()}_out.mp4"

    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    ffmpeg_cmd = [
        "ffmpeg", "-y", "-i", str(input_path), "-an",
        "-vf",
        f"scale=-1:1080,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawtext=text='{text}':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=50",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(output_path)
    ]

    subprocess.run(ffmpeg_cmd, check=True)

    return FileResponse(output_path, filename="processed.mp4")
