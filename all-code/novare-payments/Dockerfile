# Dockerfile
FROM python:3.12.10-slim

# set env
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/usr/src/app

# create a user
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR $APP_HOME

# system deps for some packages and curl (for health checks), gzip cleanup
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# copy dependency specification first for caching
COPY requirements.txt .

# install deps
RUN python -m pip install --upgrade pip wheel setuptools
RUN pip install --no-cache-dir -r requirements.txt

# copy app
COPY . .

# make a directory for logs and set ownership
RUN mkdir -p /var/log/payment_system && chown -R appuser:appuser /var/log/payment_system

# expose port
EXPOSE 8000

# healthcheck (Docker will know container is alive)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -f http://127.0.0.1:8000/health || exit 1

# use Gunicorn with Uvicorn workers (4 workers - tune to CPU)
CMD ["gunicorn", "main:app", \
      "-k", "uvicorn.workers.UvicornWorker", \
      "--bind", "0.0.0.0:8000", \
      "--workers", "4", \
      "--worker-connections", "1000", \
      "--timeout", "120", \
      "--log-level", "info", \
      "--access-logfile", "-", \
      "--error-logfile", "-"]
