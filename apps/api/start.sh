#!/bin/sh
set -e
mkdir -p var/resumes

redis-server --bind 127.0.0.1 --save "" --appendonly no --maxmemory 128mb &

alembic upgrade head
python -m scripts.seed_skills || echo "seed_skills failed, continuing"

celery -A app.worker worker --loglevel=info --concurrency=1 &

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips="*"