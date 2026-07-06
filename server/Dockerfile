# Облачная сборка Фонотеки: движок поиска/скачивания на всегда-включённом сервере.
# Подходит для Render / Koyeb / Fly / Railway и любого хостинга с Docker.
FROM python:3.12-slim

# ffmpeg — на будущее (склейка/конвертация), крохотный слой
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fono_server.py fonoteka.html ./

# В облаке YouTube не работает (блокирует дата-центры) — отключаем его и
# ютуб-зависимые каталоги. Остаются Rutube / SoundCloud / Bandcamp.
ENV FONO_NO_YT=1

# Хостинг сам задаёт порт через переменную PORT (fono_server.py её читает).
CMD ["python", "fono_server.py"]
