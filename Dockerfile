FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
# git is required for pip to install the truage-core dependency (git+https://…)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install -r requirements.txt

COPY . .

# /tmp is writable and used for the pull JSON + generated report
RUN mkdir -p /tmp

EXPOSE 5001

CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-5001} --workers 1 --timeout 180 --access-logfile - --error-logfile -"]
