# Dune: Awakening — Pelican egg (native Linux, Docker)

A [Pelican](https://pelican.dev/) (and [Pterodactyl](https://pterodactyl.io/)) egg
that runs a **full Dune: Awakening dedicated server battlegroup on Linux Docker**
— no Hyper-V, no Kubernetes, no Windows host required.

This egg is a port of [CubeCoders' AMP template for Dune
Awakening](https://github.com/CubeCoders/AMPTemplates/blob/main/duneawakening.kvp)
([CubeCoders on X](https://x.com/CubeCoders/status/2054253569738506359)) to the
Wings runtime model. CubeCoders did the hard reverse-engineering work; this
egg lets you use it without buying an AMP license. Their scripts are MIT-licensed
and are vendored under `scripts/` with attribution (see `LICENSE` / `NOTICE`).

## Why this exists

Funcom's official self-host bundle is a **Hyper-V VM image** plus a
**Kubernetes cluster** that runs game-server pods inside the VM. That setup
only works on Windows Pro and is wildly off-model for Pelican Wings.

But the binaries Funcom puts on Steam (the `4754530` Production depot and
`3104830` PTC depot) are **native Linux ELFs** packaged as 7 OCI image
tarballs:

| OCI image | Contents |
|---|---|
| `igw-postgres` | Postgres 17.4 (musl) |
| `server-rabbitmq` | RabbitMQ 3.13 + Erlang OTP 26 (musl) |
| `server-bg-director` | Battlegroup Director (.NET AOT, musl) |
| `server-text-router` | Text Router (.NET AOT, musl) |
| `server-gateway` | Gateway Service (Python 3.12, musl) |
| `server-db-utils` | ToolsDB / resetdb (Python 3.12, musl) |
| `server` | UE5 dedicated server (glibc) |

The trick is twofold:

1. `patchelf --set-interpreter` each musl-linked binary so it uses its own
   image's bundled `ld-musl-x86_64.so.1` loader — letting them run on Debian
   glibc.
2. Ship a small Go **mock Kubernetes API** (`scripts/mock-k8s-go`) that the
   Battlegroup Director talks to instead of a real K8s cluster. The director
   thinks it's launching pods; mock-k8s spawns UE5 server processes directly.

The Pelican egg wires this up against Wings instead of AMP.

## Status

- **Phase 1 done**: project scaffolded with the upstream MIT scripts vendored.
- **Phase 2 done**: `egg-dune-awakening.json` — Pelican PLCN_v3 egg, 209 lines,
  12 variables, embedded install script (SteamCMD anonymous → CubeCoders'
  scripts tarball → `install.sh` patchelf pipeline).
- **Phase 3 done**: `docker/Dockerfile` — Debian Bookworm-slim runtime with
  the 14 apt deps + tini PID 1 + pre-created K8s ServiceAccount mount with
  `container` UID 988 ownership.
- **Phase 4 done**: verification — shellcheck 0 errors on scripts + embedded
  install script, jq parse clean, runtime image smoke test green.
- **Phase 5 done**: image published to
  [`ghcr.io/sergentval/pelican-dune-awakening:latest`](https://github.com/Sergentval/pelican-egg-dune-awakening/pkgs/container/pelican-dune-awakening).
  ⚠️ Until the package is toggled public via the GHCR web UI, Wings hosts
  pulling the image need `docker login ghcr.io`.
- Phase 6 _(remaining)_: end-to-end smoke test with a real `DUNE_JWT` — see
  [`TESTING.md`](./TESTING.md). Path A install step currently blocked on a
  SteamCMD bootstrap TLS issue (2017-era binary against modern Steam CDN);
  the egg's pipeline is correct, but a `LD_PRELOAD` of system libcurl in
  the install script may be needed to recover.

## Project layout

```
pelican-egg-dune-awakening/
├── README.md                    ← you are here
├── LICENSE                      ← MIT (upstream CubeCoders + this fork's edits)
├── NOTICE                       ← attribution
├── egg-dune-awakening.json      ← the Pelican egg (import this into the panel)
├── docker/
│   ├── Dockerfile               ← runtime image (build + push before importing)
│   ├── README.md                ← build / push / smoke-test instructions
│   └── .dockerignore
└── scripts/                     ← MIT CubeCoders launch scripts (vendored)
    ├── UPSTREAM-README.md       ← upstream readme from CubeCoders/AMPTemplates
    ├── pelican-entrypoint.sh    ← THIS REPO's only contribution to scripts/
    ├── install.sh, console.sh, prestart.sh, lib.sh, ...  (14 .sh files)
    ├── mock-k8s-go              ← 6.9MB Go binary: mock Kubernetes API
    └── templates/director.ini
```

## Hardware requirements (per battlegroup)

- 6 CPU cores minimum / 8 recommended (AVX2 required)
- 32 GB RAM minimum / 64 GB recommended
- ~50 GB disk (depot + state)

A "battlegroup" is one world. It hosts multiple "Sietches" (always-warm hub
maps + on-demand instances for dungeons / story missions / Deep Desert) on
a shared UDP port pool.

## Networking

| Protocol | Port(s) | Purpose |
|---|---|---|
| TCP | 5673 | RabbitMQ Game broker (AMQPS / TLS) |
| TCP | 15673 | RabbitMQ Game HTTP management |
| UDP | 7777–7806 | UE5 dedicated server pool (30 slots, default 8 active) |

These are advertised to clients via Funcom's FLS service using
`DUNE_EXTERNAL_IP` (your public WAN IP).

## Setup (once the egg is import-ready)

1. Get your **Self-Host Service Token** from
   [account.duneawakening.com](https://account.duneawakening.com/) (or
   `account-pts.duneawakening.com` for the PTC branch). Sign in with the
   Steam account that owns Dune: Awakening. **Each running server needs its
   own unique token.**
2. Build and push the runtime Docker image (see Phase 3) to your registry.
3. In the Pelican panel: **Admin → Eggs → Import** → upload
   `egg-dune-awakening.json` → create a new server, paste the token into
   `DUNE_JWT`, set world title and region, deploy.

## Credits

- **CubeCoders Limited** ([CubeCoders/AMPTemplates](https://github.com/CubeCoders/AMPTemplates))
  — reverse-engineered the architecture, wrote the install/start scripts,
  and the mock-k8s shim. Their work is licensed MIT and is what makes this
  egg possible.
- **Funcom** — ships the actual server binaries.
- This Pelican port — `valentin95150@hotmail.fr`.

## References

- AMP Dune Awakening setup guide:
  [discourse.cubecoders.com/t/dune-awakening-server-guide/40200](https://discourse.cubecoders.com/t/dune-awakening-server-guide/40200)
- Funcom self-host docs:
  [duneawakening.com/self-hosted-servers](https://duneawakening.com/self-hosted-servers/)
- Community Rust manager (Hyper-V + SSH-to-Ubuntu):
  [adainrivers/dune-dedicated-server-manager](https://github.com/adainrivers/dune-dedicated-server-manager)
