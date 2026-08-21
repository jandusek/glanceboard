# syntax=docker/dockerfile:1

# ─── Stage 1: build the Vite dashboard ──────────────────────────
FROM node:24-alpine AS web

WORKDIR /build

# Lockfile-only install first, so dependency layers cache across source edits.
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/ ./
# The prebuild script creates src/config.js from the template if absent.
# It only carries Firebase config for the hosted variant; self-hosted ignores it.
RUN npm run build


# ─── Stage 2: runtime ───────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Python 3.11, not 3.12+: server/requirements.txt pins numpy==2.0.2 and
# Pillow==11.3.0, which have no wheels for newer interpreters and would
# fall back to a source build.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

# Optional Gmail digest widget — off by default, see EMAIL_SETUP.md.
ARG INSTALL_EMAIL=false

WORKDIR /app/server

COPY server/requirements.txt server/requirements-email.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$INSTALL_EMAIL" = "true" ]; then \
        pip install --no-cache-dir -r requirements-email.txt; \
    fi

COPY server/ ./

# app.py mounts "../web/dist" relative to its own working directory
# (app.py:2784), so the built dashboard must sit as a sibling of server/.
COPY --from=web /build/dist /app/web/dist

# Ship the template but never a real config — data/ is a volume, and
# config.json holds the Gemini API key.
RUN rm -f data/config.json

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4).status==200 else 1)"

# WORKDIR must stay /app/server: data paths in app.py are relative
# ("data/images", "data/config.json"), not absolute.
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
