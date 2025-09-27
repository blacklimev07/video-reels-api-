# Video Reels API

Сервис для обработки видео под Reels:
- убирает звук
- вставляет горизонтальное видео в вертикальный кадр
- накладывает белый текст сверху

## Запуск

```bash
docker build -t video-reels-api .
docker run -p 8000:8000 video-reels-api
