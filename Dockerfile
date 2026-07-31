# Multi-stage build: the frontend is built inside the image so the
# served bundle can never go stale relative to the source (see the
# Phase 7 deployment brief) — no local `npm run build` step feeds this.

FROM node:20-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY engine/ ./engine/
COPY api/ ./api/
COPY scripts/ ./scripts/
COPY sql/ ./sql/

# api/main.py mounts frontend/dist relative to the repo root (see the
# _FRONTEND_DIST path in api/main.py), so it lands at the same relative
# location inside the image.
COPY --from=frontend-build /frontend/dist ./frontend/dist

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Azure Container Apps injects $PORT; default 8000 matches local dev.
# Shell form so ${PORT} actually expands.
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
