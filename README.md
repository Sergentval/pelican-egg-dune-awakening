# Dune: Awakening — Pelican egg (native Linux, Docker)

A [Pelican](https://pelican.dev/) (and [Pterodactyl](https://pterodactyl.io/)) egg
that runs a **full Dune: Awakening dedicated server battlegroup on Linux Docker**
— no Hyper-V, no Kubernetes, no Windows host required, no AMP runtime.

This project is **Pelican-native**. The architecture was originally
reverse-engineered by [CubeCoders Limited](https://cubecoders.com/) for their
AMP product and published as MIT-licensed scripts at
[CubeCoders/AMPTemplates](https://github.com/CubeCoders/AMPTemplates). We
forked their scripts, ported the model to Pelican Wings, rewrote
`mock-k8s-go` as an open-source Go binary (their original was closed and
refused to run outside AMP), and added a panel-driven config-applier for
22 game-side tunables (loot multipliers, sandstorms, sandworms, PvP zones,
building limits, on-demand pool tuning). See [`ATTRIBUTION.md`](./ATTRIBUTION.md)
for the full credit + license discussion.

## How it works

Funcom's official self-host bundle is a Hyper-V VM image plus a Kubernetes
cluster that runs game-server pods inside the VM. That setup only works on
Windows Pro.

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
2. A small Go **mock Kubernetes API** (`scripts/mock-k8s-go`) that the
   Battlegroup Director talks to instead of a real K8s cluster. The Director
   thinks it's launching pods; mock-k8s spawns UE5 server processes directly.

The egg wires this up against Wings.

## Status

The egg has been running a production world end-to-end (real players,
characters created, persistent save). The current code path:

- `egg-dune-awakening.json` — Pelican PLCN_v3 egg, 36 panel variables
  (FLS token + 22 game-side tunables + infrastructure ports). Install
  script fetches our repo tarball from GitHub, no upstream race.
- `docker/Dockerfile` — Debian Bookworm-slim runtime with required apt
  deps, tini as PID 1, K8s ServiceAccount mount declared as VOLUME.
  Published to
  [`ghcr.io/sergentval/pelican-dune-awakening:latest`](https://github.com/Sergentval/pelican-egg-dune-awakening/pkgs/container/pelican-dune-awakening).
- `scripts/` — vendored fork of CubeCoders' launch scripts with our
  Pelican-specific fixes (graceful shutdown grace periods, Funcom DB
  migration regression workarounds, partition ID handling, etc.). All
  AMP-isms stripped.
- `mock-k8s/` — open-source Go re-implementation of CubeCoders'
  closed mock-k8s binary, built from source at install time.
- `scripts/apply-config.sh` + `scripts/pelican-entrypoint.sh` — our own
  contributions for Pelican-side panel-variable substitution and
  foreground orchestration.

## Project layout

```
pelican-egg-dune-awakening/
├── README.md                    ← you are here
├── ATTRIBUTION.md               ← credit + MIT lineage
├── LICENSE                      ← MIT (CubeCoders + this fork's edits)
├── NOTICE                       ← attribution summary
├── egg-dune-awakening.json      ← the Pelican egg (import into the panel)
├── docker/
│   ├── Dockerfile               ← runtime image
│   ├── README.md                ← build / push / smoke-test instructions
│   └── .dockerignore
├── mock-k8s/                    ← our open-source Go replacement
│   ├── cmd/mock-k8s/main.go
│   ├── go.mod, go.sum
│   └── internal/...
└── scripts/                     ← vendored from CubeCoders, modified
    ├── UPSTREAM-README.md       ← historical CubeCoders README
    ├── pelican-entrypoint.sh    ← our foreground orchestrator
    ├── apply-config.sh          ← our panel-variable INI applier
    ├── install.sh, console.sh, prestart.sh, lib.sh, ...  (14 .sh files)
    └── templates/director.ini
```

Note: `scripts/mock-k8s-go` is not in git. It is built deterministically
in the install container from `mock-k8s/cmd/mock-k8s/` with
`CGO_ENABLED=0 go build -trimpath -ldflags='-s -w'`.

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

## Setup

1. Get your **Self-Host Service Token** from
   [account.duneawakening.com](https://account.duneawakening.com/) (or
   `account-pts.duneawakening.com` for the PTC branch). Sign in with the
   Steam account that owns Dune: Awakening. **Each running server needs its
   own unique token.**
2. Build and push the runtime Docker image (see [`docker/README.md`](./docker/README.md))
   to a registry your Wings host can pull from, or use the published
   `ghcr.io/sergentval/pelican-dune-awakening:latest`.
3. In the Pelican panel: **Admin → Eggs → Import** → upload
   `egg-dune-awakening.json` → create a new server, paste the token into
   `DUNE_JWT`, set world title and region, deploy.

The install fetches scripts + Go source from this repo's `main` branch by
default. Pin a tag or commit SHA via the `DUNE_EGG_REF` panel variable for
reproducible installs.

## Credits

See [`ATTRIBUTION.md`](./ATTRIBUTION.md) for the full credit list and MIT
lineage. Short version:

- **CubeCoders Limited** ([CubeCoders/AMPTemplates](https://github.com/CubeCoders/AMPTemplates))
  reverse-engineered the architecture and published the launch scripts
  under MIT. This egg would not exist without that work.
- **Funcom** ships the actual server binaries via Steam.
- This Pelican port — `valentin95150@hotmail.fr`.

## References

- AMP Dune Awakening setup guide:
  [discourse.cubecoders.com/t/dune-awakening-server-guide/40200](https://discourse.cubecoders.com/t/dune-awakening-server-guide/40200)
- Funcom self-host docs:
  [duneawakening.com/self-hosted-servers](https://duneawakening.com/self-hosted-servers/)
- Community Rust manager (Hyper-V + SSH-to-Ubuntu):
  [adainrivers/dune-dedicated-server-manager](https://github.com/adainrivers/dune-dedicated-server-manager)
