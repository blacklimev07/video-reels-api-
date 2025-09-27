FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
