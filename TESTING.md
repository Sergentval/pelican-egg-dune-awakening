# Testing the egg before opening upstream PRs

You have three paths, lightest first. Path A is enough to gate the PRs.

## Prerequisites

- A valid **Self-Host Service Token** from
  [account.duneawakening.com](https://account.duneawakening.com/) (retail)
  or [account-pts.duneawakening.com](https://account-pts.duneawakening.com/) (PTC).
  Without it `prestart.sh` aborts on line ~104. The token can't be faked or
  zero-stubbed because the script decodes the JWT and reads `HostId`.
- ~20 GB free disk (depot ~5 GB, extracted rootfs ~12 GB, state grows).
- 32 GB RAM available to the container.
- Your **public WAN IP** (needed for FLS to register the server).
- Router/firewall forwarding for `7777-7806/UDP`, `5673/TCP`, `15673/TCP`
  pointing at the test box. Without this, the server boots fine locally
  but never appears in the in-game server browser.

## Path A — Manual smoke test on this VPS (no Pelican panel)

Tests every layer the PR depends on: install pipeline, yolk image build,
boot orchestration, FLS handshake. Stops short of an actual client
connection (which needs the game on a Steam machine).

```bash
# 0. Variables you'll re-use
export DUNE_TEST_DIR=~/dune-test-server
export DUNE_JWT='paste-the-token-here'
export DUNE_EXTERNAL_IP="$(curl -s ifconfig.me)"

# 1. Build the yolk image locally
cd ~/projects/upstream/pelican-yolks-fork
docker build -t pelican-dune-awakening:test games/dune_awakening/

# 2. Run the install script in the official Pelican installer image,
#    writing into a local volume that simulates Wings' /mnt/server.
mkdir -p "$DUNE_TEST_DIR"
python3 -c "
import yaml
egg = yaml.safe_load(open('$HOME/projects/upstream/pelican-games-steamcmd-fork/dune_awakening/egg-dune-awakening.yaml'))
print(egg['scripts']['installation']['script'])
" > /tmp/dune-install.sh

docker run --rm \
    --user 0:0 \
    -v "$DUNE_TEST_DIR":/mnt/server \
    -v /tmp/dune-install.sh:/install.sh:ro \
    -e SRCDS_APPID=4754530 \
    -e STEAM_USER=anonymous \
    -e STEAM_PASS= \
    -e STEAM_AUTH= \
    ghcr.io/parkervcp/installers:debian \
    bash /install.sh

# Sanity-check the layout the runtime will see
ls -la "$DUNE_TEST_DIR"/{scripts,extracted,server/images/battlegroup}/ | head -20

# 3. Boot the runtime against the installed dir
docker run --rm -it \
    --name dune-test \
    --user 988:988 \
    -v "$DUNE_TEST_DIR":/home/container \
    -e STARTUP='bash scripts/console.sh /home/container' \
    -e DUNE_JWT="$DUNE_JWT" \
    -e DUNE_WORLD_TITLE='Local Smoke Test' \
    -e DUNE_REGION='Europe' \
    -e DUNE_HOST_DC_ID='local-test' \
    -e DUNE_BIND_IP='0.0.0.0' \
    -e DUNE_EXTERNAL_IP="$DUNE_EXTERNAL_IP" \
    -e DUNE_MQ_GAME_PORT=5673 \
    -e DUNE_MQ_GAME_MGMT_PORT=15673 \
    -e K8S_POOL_GAME_PORT_BASE=7777 \
    -e DUNE_RELEASE_VERSION=4754530 \
    -p 5673:5673 \
    -p 15673:15673 \
    -p 7777-7806:7777-7806/udp \
    pelican-dune-awakening:test
```

### What success looks like

You should see (over several minutes):

```
[entrypoint] [INFO] Dune Awakening — Pelican boot sequence starting
[prestart] [INFO] Verifying world identity...
[prestart] [INFO]   HostId: <hex-from-JWT>
[postgres] [INFO] Postgres ready: success
[mq-admin] [INFO] RabbitMQ Admin broker ready: success
[mq-game] [INFO] RabbitMQ Game broker (TLS) ready: success
[text-router] [INFO] Text router ready: success
[mock-k8s] [INFO] Mock Kubernetes API ready: success
[director] [INFO] Battlegroup Director ready: success
[gateway] [INFO] Gateway ready: success
[amp] [INFO] Dune Awakening server ready: success     <-- pass marker
```

After "ready: success", mock-k8s starts spawning UE5 instances for the
always-warm maps (Survival_1, Overmap, DeepDesert_1). Each takes a
couple of minutes to bind their UDP port. Watch for:

```
[ue5-Survival_1] [INFO] UE5 server (Survival_1) ready: success
[ue5-Overmap]    [INFO] UE5 server (Overmap) ready: success
[ue5-DeepDesert_1] [INFO] UE5 server (DeepDesert_1) ready: success
```

### Common failure modes and what they mean

| Failure | Meaning | Fix |
|---|---|---|
| `Self-Host Service Token not set` | `DUNE_JWT` env var is empty | Paste a real token |
| `couldn't decode HostId from JWT` | Token is malformed or expired | Get a fresh one from account.duneawakening.com |
| `extracted prerequisites missing` | Install step didn't finish | Re-run step 2 with more disk / RAM |
| `Mock Kubernetes API failed to listen within 15s` | mock-k8s-go crashed | Check `$DUNE_TEST_DIR/logs/mock-k8s.log` |
| `wait_for_udp_bind` timeouts (UE5 fails to start) | UE5 binary failed to launch | Check `$DUNE_TEST_DIR/logs/ue5-*.log` |

### Cleanup

```bash
docker rm -f dune-test 2>/dev/null
sudo rm -rf "$DUNE_TEST_DIR"
docker rmi pelican-dune-awakening:test
```

## Path B — Run a Pelican panel locally

Tests the egg import flow as upstream maintainers will. Heavier setup.

```bash
# Pelican publishes an automated installer
bash <(curl -s https://pelican.dev/install.sh)

# After install, log in at https://<your-vps-ip>/admin
# Admin → Eggs → Import → upload egg-dune-awakening.yaml
# Allocate UDP 7777-7806 + TCP 5673, 15673 to a new server
# Create server, paste JWT, deploy
```

The panel will pull `ghcr.io/pelican-eggs/games:dune_awakening` from
GHCR — which doesn't exist until your **yolk PR** is merged and CI
publishes it. So for this path:

1. Push your yolk fork to your own GHCR (build it locally and tag as
   `ghcr.io/<yourgithub>/games:dune_awakening`).
2. Edit the egg YAML's `docker_images` to point at YOUR image.
3. Re-export the egg from the panel after testing.
4. Restore the egg's `docker_images` to `ghcr.io/pelican-eggs/games:dune_awakening`
   before opening the PR.

## Path C — Ask the Pelican community to test

Once you've passed Path A, the Pelican Discord
([pelican.dev/discord](https://pelican.dev/discord)) has volunteers
who'll test draft PRs on their own panels. Open both PRs as **draft**,
post in their `#egg-testing` channel, and they'll exercise the panel
import + first boot for you.

## Offline tests (no server, no token, seconds to run)

Run these first — they need neither the depot nor a Funcom token, so a
failure here is never worth booting a server to investigate.

```bash
for t in scripts/test_*.py; do python3 "$t" || break; done  # pure-logic units
bash scripts/test_console_panel.sh                          # console `panel` commands (~40s)
```

`test_console_panel.sh` boots the real `console.sh` against a throwaway
fake container and drives the stdin listener, so it covers the whole
supervisor loop rather than a function in isolation.

## Minimum bar before opening the PRs

- [ ] Offline tests above all pass.
- [ ] Path A end-to-end clean: `Dune Awakening server ready: success`
  appears in the log.
- [ ] All three always-warm UE5 maps spawn and bind their UDP ports.
- [ ] FLS registration succeeded (search for `LogFLS:.*Registered` in
  `logs/ue5-Survival_1.log`).
- [ ] One client connection succeeds end-to-end (only confirmable from
  a machine running the Steam game).

If anything in steps 1-3 fails, the PRs aren't ready. Step 4 can be
asked from the Pelican Discord since it needs a different machine.

## Tip: keep the test artifacts to debug PR feedback

When maintainers review the PR they'll ask questions like "did
RabbitMQ TLS handshake succeed?" or "what was the mock-k8s pool size?".
Keep `$DUNE_TEST_DIR/logs/` around after the test so you can grep it.
