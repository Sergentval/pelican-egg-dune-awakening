# syntax=docker/dockerfile:1.7
#
# Runtime image for the Dune: Awakening Pelican egg.
#
# What this provides that off-the-shelf Pelican yolks don't:
#   1. /var/run/secrets/kubernetes.io/serviceaccount/ pre-created with
#      container-user ownership. The Funcom Battlegroup Director reads
#      its ServiceAccount token from there, mock-k8s-go writes it there,
#      and prestart.sh dies if it isn't writable. AMP solves this with a
#      root-run customstart.sh hook on every container boot; Pelican Wings
#      runs unprivileged at runtime, so we bake the directory + ownership
#      into the image.
#   2. The 12 runtime dependencies the CubeCoders launch scripts need
#      (bash, jq, gawk, openssl, file, python3, sed, grep, iproute2,
#      util-linux, gzip, tar, ca-certificates, procps).
#   3. tini as PID 1, with -g for process-group signal forwarding — the
#      start scripts background ~10 services via setsid; without proper
#      reaping we leak zombies on every container restart.
#
# Built and pushed in CI to:
#   ghcr.io/sergentval/pelican-dune-awakening:latest
#
# See ./README.md for local build + push instructions.

FROM debian:bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/Sergentval/pelican-egg-dune-awakening"
LABEL org.opencontainers.image.description="Pelican Wings runtime for Dune: Awakening dedicated server"
LABEL org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# Distro-stable packages — pinning them would break every Debian point release
# and add no real reproducibility (we don't ship deterministic builds anyway).
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        coreutils \
        curl \
        file \
        gawk \
        grep \
        gzip \
        iproute2 \
        jq \
        openssl \
        procps \
        python3 \
        sed \
        tar \
        tini \
        tzdata \
        util-linux \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Pelican convention: runtime user is `container` at UID 988 with home at
# /home/container. Matches ghcr.io/parkervcp/yolks images so other Pelican
# tooling that hard-codes this UID continues to work.
RUN useradd --uid 988 --user-group --create-home --home-dir /home/container --shell /bin/bash container

# K8s ServiceAccount mount — see header. The Funcom Director's IGWO
# component reads /var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt}
# at the standard path; mock-k8s-go writes them there as the container user.
#
# Pelican Wings starts containers with ReadonlyRootfs=true and only mounts
# /home/container and /tmp as writable. Image-built directories anywhere
# else are invisible — the runtime can't write to them. The fix is to
# declare this path as a Docker VOLUME: at container creation Docker mounts
# an anonymous writable volume here and copies the image's initial content
# (this empty 0777 dir) into it. The 0777 mode is necessary because Wings
# may resolve the runtime UID dynamically, and chown'ing in the image is
# moot once Docker is operating on a fresh anonymous volume.
RUN mkdir -p /var/run/secrets/kubernetes.io/serviceaccount \
 && chmod 0777 /var/run/secrets/kubernetes.io/serviceaccount
VOLUME ["/var/run/secrets/kubernetes.io/serviceaccount"]

USER container
WORKDIR /home/container
STOPSIGNAL SIGTERM

# tini as init: forwards signals to the whole process group (-g) so the
# console.sh trap actually fires across the 10+ background services it
# orchestrates, and reaps any zombies left behind by setsid'd children.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
# Pelican Wings passes the egg's startup command as $STARTUP and starts the
# container with the image's default CMD. The CMD must therefore eval
# $STARTUP so Wings can actually launch the server. Falls back to a plain
# shell if STARTUP is unset (useful for SFTP-only / debugging sessions).
CMD ["/bin/bash", "-c", "exec ${STARTUP:-bash}"]
