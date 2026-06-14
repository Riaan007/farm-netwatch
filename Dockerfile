# syntax=docker/dockerfile:1

# --- Stage 1: build the Tailwind CSS once, natively on the build host ---------
# Runs on the BUILD platform (not emulated) since CSS output is arch-independent.
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS cssbuilder
ARG BUILDARCH
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY app/static ./static
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
    NETWATCH_DATA=/data \
    NETWATCH_PORT=8090
# nmap = scanner; iproute2 = `ip` for local-subnet detection;
# iputils-ping = connectivity-quality test (loss / latency / jitter)
# nmap = scanner; iproute2 = `ip`; iputils-ping = connectivity test;
# wireguard-tools = browser-driven "Connect to Central Hub" (wg-quick/wg in the
# host netns — the site dials out to the hub, opening NO inbound ports).
RUN apt-get update && apt-get install -y --no-install-recommends \
      nmap iproute2 iputils-ping wireguard-tools ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/
COPY --from=cssbuilder /build/static/app.css /app/static/app.css
# Build the offline OUI table from nmap's bundled vendor list (no external fetch).
# Use Python (not awk) so we don't depend on mawk interval-regex support.
RUN mkdir -p /app/data && python3 - <<'PY'
src = "/usr/share/nmap/nmap-mac-prefixes"
out = "/app/data/oui.tsv"
n = 0
with open(src, encoding="utf-8", errors="replace") as f, open(out, "w") as o:
    for line in f:
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2 or len(parts[0]) != 6:
            continue
        try:
            int(parts[0], 16)
        except ValueError:
            continue
        o.write(parts[0].upper() + "\t" + parts[1] + "\n")
        n += 1
print("oui.tsv rows:", n)
PY
EXPOSE 8090
VOLUME ["/data"]
CMD ["python", "server.py"]
