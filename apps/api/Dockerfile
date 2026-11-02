FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/srv/apps/api/.venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends redis-server \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv

# settings.py looks for .env four levels up, so keep the repo's folder depth.
WORKDIR /srv/apps/api

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

EXPOSE 8000
CMD ["sh", "start.sh"]