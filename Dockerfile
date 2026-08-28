FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg kodiert die Übergangsclips, die Schriftpakete liefern die Typografie
# dafür (Liberation bevorzugt, DejaVu als Rückfall).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite liegt im Volume /app/data
RUN mkdir -p /app/data /app/data/transitions && \
    useradd --create-home --uid 1000 ptm && \
    chown -R ptm:ptm /app
USER ptm

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
