FROM node:22-alpine AS dashboard-build
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
RUN pip install '.[browser]' && python -m playwright install --with-deps chromium && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY --from=dashboard-build /build/web/dist /app/web-dist
RUN useradd --create-home --uid 10001 takumi && mkdir -p /data && chown -R takumi:takumi /app /data /ms-playwright
USER takumi
ENV DATA_DIR=/data WEB_DIST_DIR=/app/web-dist
EXPOSE 8000
ENTRYPOINT ["takumi"]
