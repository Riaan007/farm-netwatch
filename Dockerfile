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

# --- Stage 2: build a current mac-telnet (MAC-address login to RouterOS) ------
# The Debian mactelnet (0.6.1) predates RouterOS 6.43's elliptic-curve secure
# login ("mtwei", Curve25519 EC-SRP) and SEGFAULTS on connect to modern RouterOS.
# mtwei support lives only on upstream master, so we pin a commit and compile it.
# No --platform: built per-target-arch so the binary matches the runtime image.
FROM debian:bookworm-slim AS mactelnet
ARG MACTELNET_COMMIT=de620882c73e235ab319f3cb55481888d7d8f71d
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential autoconf automake autopoint gettext \
      libssl-dev zlib1g-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN git clone https://github.com/haakonnessjoen/MAC-Telnet.git \
    && cd MAC-Telnet \
    && git checkout "${MACTELNET_COMMIT}" \
    && autopoint --force && autoreconf -fi \
    && ./configure \
    && make -j"$(nproc)" \
    && strip src/mactelnet

# --- Stage 2b: go2rtc, the live-video relay (Phase 7) -------------------------
# A small static program (Apache-2.0, github.com/AlexxIT/go2rtc) that takes a camera's
# RTSP stream and hands it to a browser as MP4 — it repackages, it does not decode,
# so a Pi 3 copes. Netwatch starts it only while someone watches (app/camlive.py),
# listening on 127.0.0.1 only. Pinned version AND file hashes: a changed file fails
# the build.
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS go2rtc
ARG TARGETARCH
ARG TARGETVARIANT
ARG GO2RTC_VERSION=v1.9.4
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "${TARGETARCH}${TARGETVARIANT}" in \
      amd64)  F=go2rtc_linux_amd64; H=8d86510e64e0deadee40d550d46323ddec9f62e14cec56de3a9df0f2f7fe9ada ;; \
      arm64)  F=go2rtc_linux_arm64; H=ebea4cf3a0bc3a12190ebba1f2c4b3c8cf4aac099fadb4cd50a669429b074f1c ;; \
      armv7)  F=go2rtc_linux_arm;   H=a40459ab24d3ce5e329226adc905e5192dffccbb53d42de06d3ee3f2ebf2ebc6 ;; \
      *) echo "no go2rtc for ${TARGETARCH}${TARGETVARIANT}"; exit 1 ;; \
    esac; \
    curl -fsSL -o /go2rtc "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/${F}"; \
    echo "${H}  /go2rtc" | sha256sum -c -; \
    chmod 0755 /go2rtc

# --- Stage 3: the runtime image ----------------------------------------------
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
      openssh-client sshpass smartmontools libssl3 \
    && rm -rf /var/lib/apt/lists/*

# mac-telnet: WinBox-style login to a MikroTik by MAC address (built in stage 2).
# libssl3 above is its only runtime dep (mtwei uses OpenSSL EC). See mikrotik.py.
COPY --from=mactelnet /build/MAC-Telnet/src/mactelnet /usr/local/bin/mactelnet
# go2rtc: live video (stage 2b), started on demand by app/camlive.py.
COPY --from=go2rtc /go2rtc /usr/local/bin/go2rtc

# OPT-IN: DHCP<->static IP control via NetworkManager. The lean :latest image leaves
# this OFF (INSTALL_NM=0) to stay small. Build with --build-arg INSTALL_NM=1 (see
# docker-compose.netcfg.yml) to add `nmcli`; you must ALSO mount the host D-Bus socket
# for it to reach NetworkManager. Without it, netcfg feature-detects and hides static.
ARG INSTALL_NM=0
RUN if [ "$INSTALL_NM" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends network-manager \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# OPT-IN: ffmpeg, for the RTSP one-frame snapshot of a non-Hikvision camera whose
# HTTP snapshot is broken (app/camrtsp.py — e.g. the Dynacolor fisheye, which
# advertises an ONVIF snapshot URL that 404s). OFF by default so the pulled GHCR
# images (the farm sites) stay lean; build --build-arg INSTALL_FFMPEG=1 on a site
# that has such a camera (the home site builds locally). The engine feature-detects
# ffmpeg (camrtsp.available) and only offers the own-rtsp route when it is present.
ARG INSTALL_FFMPEG=0
RUN if [ "$INSTALL_FFMPEG" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends ffmpeg \
      && rm -rf /var/lib/apt/lists/*; \
    fi
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
