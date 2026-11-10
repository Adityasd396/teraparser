FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright RIPPER_BIND_HOST=0.0.0.0 PORT=5050 TERABOX_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 parser \
    && install -d -m 700 -o parser -g parser /data
COPY app.py ./
COPY assets/ ./assets/
USER parser
EXPOSE 5050
CMD ["python", "app.py"]
