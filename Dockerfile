FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# v1.2.0
COPY app.py .

RUN mkdir -p /data

ENV DATABASE_PATH=/data/overlord.db
ENV PORT=5000

EXPOSE 5000

# 1 worker + 4 threads : compatible SQLite (mono-writer)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "app:app"]
