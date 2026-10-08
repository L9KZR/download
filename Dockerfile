FROM denoland/deno:bin AS deno
FROM python:3.12-slim-bookworm
COPY --from=deno /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py core.py store.py downloads.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
