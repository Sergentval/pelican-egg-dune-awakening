# Dune Awakening — AMP scripts (historical CubeCoders README)

> **Note from this fork:** the README below is the original document from
> CubeCoders' upstream `AMPTemplates` repo. It describes the AMP-native
> architecture as published by CubeCoders. This Pelican fork has since
> diverged: there is no AMP daemon, no PreStartStages (pelican-entrypoint.sh
> runs every stage in one shell), no customstart.sh hook (Dockerfile owns
> the SA mount), and `mock-k8s-go` is rebuilt from our open-source Go source
> at install time rather than fetched as a closed binary. See `../README.md`
> and `../ATTRIBUTION.md` for the Pelican-native architecture.

# Dune Awakening — AMP scripts

These scripts implement the Dune Awakening dedicated server for AMP.  They
replace the k3s + systemd setups we used during reverse-engineering with
an AMP-native pattern (PreStartStages + foreground console).

## Layout

Everything below is relative to `{{$FullBaseDir}}`.

```
scripts/         this directory — pulled by AMP's first update stage
depot/           SteamCMD download (Funcom IGW depot, app 3014830)
extracted/       OCI rootfs trees, written by install.sh
  ├── postgres/  Postgres 17.4 (Alpine musl)
  ├── mq/        RabbitMQ 3.13 + Erlang OTP 26 + OpenSSL
  ├── director/  Battlegroup Director (.NET AOT)
  ├── text-router/ Text Router (.NET AOT)
  ├── gateway/   Gateway Service (Python 3.12)
  ├── db-utils/  ToolsDB / resetdb (Python 3.12)
  └── game-server/ UE5 dedicated server
state/           persistent across restarts AND updates
  ├── pg/data/   Postgres PGDATA
  ├── ue5-saved/ UE5 Saved directory
  ├── world-name FLS-registered WorldName
  ├── rmq-secret RMQ HTTP-token auth secret
  ├── rmq-certs/ Self-signed TLS cert (CN = $DUNE_EXTERNAL_IP)
  └── schema-loaded marker — schema has been ToolsDB-loaded
runtime/         regenerated every container start
  ├── mq-admin/  rendered rabbitmq.conf
  ├── mq-game/   rendered rabbitmq.conf
  ├── postgres.conf, director-conf.d/
  ├── postgresql/ PG unix socket dir
  ├── pids/      PID files for every service
  └── ue5-{Survival_1,Overmap}.env
logs/            per-service log files (console.sh tails these)
```

## Phases

### Update phase (`UpdateStages` in AMP template)

1. `FetchURL` → `scripts/` (this directory, from our public repo)
2. `SteamCMD 3014830` → `depot/`
3. `Executable scripts/install.sh {{$FullBaseDir}}` — extract OCI tars, patchelf
   the musl-linked binaries.

### Start phase (`PreStartStages`)

1. `scripts/prestart.sh BASE` — JWT decode → WorldName, RMQ secret, TLS cert,
   `initdb` if first run, schema load via `resetdb.py`, seed `world_partition`,
   render runtime configs, symlink UE5 Saved dir.
2. `scripts/start-pg.sh BASE` — bg postgres + wait
3. `scripts/start-mq-admin.sh BASE` — bg admin broker + wait
4. `scripts/start-mq-game.sh BASE` — bg TLS broker + wait
5. `scripts/start-text-router.sh BASE` — bg + wait
6. `scripts/start-director.sh BASE` — bg + wait
7. `scripts/start-gateway.sh BASE` — bg + wait for "Monitoring for servers"
8. `scripts/start-ue5.sh BASE Survival_1` — bg + wait for `farm_state.ready=true`
9. `scripts/start-ue5.sh BASE Overmap` — bg + wait

### Foreground process (`App.ExecutableLinux`)

`scripts/console.sh BASE` — verifies PID files, prints a ready marker,
multiplexes service logs, and on SIGTERM stops services in reverse order.

## Env contract (set by AMP via `App.EnvironmentVariables`)

| Var | Source | Required |
|---|---|---|
| `DUNE_JWT` | `{{FlsServiceAuthToken}}` | yes |
| `DUNE_WORLD_TITLE` | `{{WorldTitle}}` | yes |
| `DUNE_REGION` | `{{Region}}` | yes |
| `DUNE_BIND_IP` | `{{$ApplicationIPBinding}}` | yes |
| `DUNE_EXTERNAL_IP` | `{{$ExternalIP}}` or `{{ManualPublicIP}}` | yes |
| `DUNE_WORLD_NAME_OVERRIDE` | `{{WorldName}}` | optional |
| `DUNE_PG_PORT`, `DUNE_MQ_*_PORT`, etc. | port-ref expansions | yes |

The full port list and AMP template JSON live in `../template/`.
