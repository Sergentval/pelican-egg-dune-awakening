# Runtime Docker image

The `Dockerfile` here builds the Pelican Wings runtime image referenced by
`../egg-dune-awakening.json`. The egg's `docker_images` field expects this
image to be pullable from a registry the Wings host can reach.

## What it provides

| Layer | Why |
|---|---|
| `debian:bookworm-slim` base | Matches CubeCoders' choice (`cubecoders/ampbase:debian`); the Funcom OCI binaries are musl-linked but bundle their own musl loader, so the host distro only needs to provide glibc for the UE5 server itself. |
| Runtime apt deps | The 14 packages the CubeCoders launch scripts call: `bash, ca-certificates, coreutils, curl, file, gawk, grep, gzip, iproute2, jq, openssl, procps, python3, sed, tar, tini, tzdata, util-linux`. |
| User `container` UID 988 | Pelican Wings convention — matches `ghcr.io/parkervcp/yolks` so existing tooling works unchanged. |
| `/var/run/secrets/kubernetes.io/serviceaccount/` pre-created with container ownership | Funcom's Battlegroup Director reads its ServiceAccount token from the standard K8s mount path; mock-k8s-go writes the token there. AMP solves this via a root-run `customstart.sh` hook before drop-privileges; Wings runs unprivileged at runtime, so we bake the path + ownership into the image. |
| `tini` as PID 1 (`-g` flag) | The start scripts background ~10 services via `setsid`; without a proper init we leak zombies on every container restart, and `console.sh`'s `SIGTERM` trap doesn't reach the whole process group on shutdown. |

## Build locally

```bash
# From the repo root
docker buildx build \
  --platform linux/amd64 \
  --tag ghcr.io/sergentval/pelican-dune-awakening:latest \
  docker/
```

The build context is empty by design (see `.dockerignore`) — only the
Dockerfile is sent to the daemon. Unrelated changes elsewhere in the repo
will never invalidate the image cache.

## Push to GHCR

```bash
# Authenticate once
echo "$GHCR_PAT" | docker login ghcr.io -u sergentval --password-stdin

# Push
docker push ghcr.io/sergentval/pelican-dune-awakening:latest
```

The image must be made **public** in GHCR's package settings, otherwise
Wings nodes need GHCR credentials to pull it.

## Use a different registry

If you want to push to your own registry instead, edit the
`docker_images` field in `egg-dune-awakening.json` **before importing**:

```json
"docker_images": {
    "Dune Awakening (custom)": "your.registry.example/dune-awakening:latest"
}
```

then rebuild with that tag and push to your registry.

## Verifying the image

Quick smoke test of the most Pelican-specific behaviours:

```bash
docker run --rm --user 988:988 ghcr.io/sergentval/pelican-dune-awakening:latest \
    bash -c '
        echo "uid=$(id -u) gid=$(id -g) home=$HOME pwd=$(pwd)"
        ls -ld /var/run/secrets/kubernetes.io/serviceaccount
        touch /var/run/secrets/kubernetes.io/serviceaccount/.probe && echo "writable: OK"
        for c in bash jq gawk openssl file python3 ip setsid tar gzip; do
            command -v "$c" >/dev/null && echo "  $c: $(command -v $c)" || echo "  $c: MISSING"
        done
    '
```

Expected output: `uid=988 gid=988 home=/home/container pwd=/home/container`,
the ServiceAccount dir owned by `container`, writable, and every binary
present.
