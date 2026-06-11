# syntax=docker/dockerfile:1
# Build context is the REPO ROOT (see hub/docker-compose.yml) so this stage can
# reuse the same tailwind.config.js / input.css as the site image.

# --- Stage 1: build the Tailwind CSS once, natively on the build host ---------
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS cssbuilder
ARG BUILDARCH
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY hub/app/static ./static
COPY tailwind.config.js input.css ./
RUN set -eux; \
    case "${BUILDARCH}" in \
      amd64) TW=tailwindcss-linux-x64 ;; \
      arm64) TW=tailwindcss-linux-arm64 ;; \
      arm)   TW=tailwindcss-linux-armv7 ;; \
      *)     TW=tailwindcss-linux-x64 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/${TW}"; \
    chmod +x /usr/local/bin/tailwindcss; \
    tailwindcss -c tailwind.config.js -i input.css -o ./static/app.css --minify

# --- Stage 2: the runtime image ----------------------------------------------
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    HUB_DATA=/data \
    HUB_PORT=8091
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY hub/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hub/app/ /app/
COPY --from=cssbuilder /build/static/app.css /app/static/app.css
EXPOSE 8091
VOLUME ["/data"]
CMD ["python", "server.py"]
