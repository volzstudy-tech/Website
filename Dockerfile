FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=5000 HOST=0.0.0.0 DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils fonts-dejavu-core libgl1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data/uploads
EXPOSE 5000
CMD ["sh", "-c", "gunicorn --workers 2 --timeout 120 --bind 0.0.0.0:${PORT:-5000} app:app"]
