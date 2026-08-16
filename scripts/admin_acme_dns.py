#!/usr/bin/env python3
"""Where the dns-01 challenge TXT gets published.

Three shapes, and the choice is a security trade-off the operator makes —
not one this code should make for them. In decreasing order of how little
the container is trusted with:

  acme-dns (self-hosted)   DUNE_ACME_DNS_BACKEND=acme-dns
      The credential can write exactly one TXT record in a throwaway
      validation zone and nothing else. Costs one more service to run.

  acme-dns (public instance)   same backend, DUNE_ACME_DNS_URL left default
      Same tight credential, no service to run — but a third party sees
      every validation and could, in principle, answer challenges for the
      delegated name. Fine for a game panel, not for a bank.

  Cloudflare API token   DUNE_ACME_DNS_BACKEND=cloudflare
      No extra service and no delegation to set up, but a Cloudflare token
      cannot be scoped narrower than a whole zone: it can rewrite every
      record in the domain. Simplest to start, widest blast radius.

The first two rely on CNAME delegation, which Let's Encrypt documents:
"you can use CNAME records or NS records to delegate answering the
challenge to other DNS zones". The operator points
_acme-challenge.panel.example.com at the validation zone once, and the
container never holds a credential for the real domain.

Stdlib only. DNS visibility is checked over DNS-over-HTTPS rather than by
shelling out to dig, which the runtime image does not ship.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ACME_DNS_PUBLIC = "https://auth.acme-dns.io"
DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query?type=TXT&name=",
    "https://dns.google/resolve?type=TXT&name=",
)


class PublishError(RuntimeError):
    """Never carries the API token — this text reaches the panel log."""


def _http(url, *, method="GET", headers=None, payload=None, timeout=30, secret=""):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Accept": "application/json", **(headers or {})})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf8") or "{}"), resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf8", "replace")[:300]
        raise PublishError(_redact(f"HTTP {exc.code} from {_host(url)}: {detail}", secret)) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PublishError(_redact(f"{_host(url)} unreachable: {exc}", secret)) from None


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url


def _redact(message: str, secret: str) -> str:
    return message.replace(secret, "<redacted>") if secret else message


def txt_visible(record: str, value: str) -> bool:
    """Is the TXT resolvable with the expected value? Checked before the CA
    is asked to look: a challenge submitted too early fails the order, and
    Let's Encrypt rate-limits failures."""
    for base in DOH_ENDPOINTS:
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(base + urllib.parse.quote(record),
                                           headers={"Accept": "application/dns-json"}),
                    timeout=15) as resp:
                answers = json.loads(resp.read().decode("utf8")).get("Answer") or []
        except (urllib.error.URLError, OSError, ValueError):
            continue
        for answer in answers:
            if value in str(answer.get("data", "")):
                return True
    return False


class _Base:
    def wait_until_visible(self, record: str, value: str, timeout: int = 300, log=print) -> bool:
        """Poll public DNS until the record shows up. Some providers take
        a minute; asking the CA before that wastes a validation attempt."""
        deadline = time.time() + timeout
        first = True
        while time.time() < deadline:
            if txt_visible(record, value):
                return True
            if first:
                log(f"waiting for {record} to propagate…")
                first = False
            time.sleep(10)
        return False


class AcmeDnsPublisher(_Base):
    """acme-dns: the container holds credentials for a validation zone that
    contains nothing but challenge records.

    Registration happens once and the credentials are persisted, because
    re-registering would hand out a new subdomain and orphan the CNAME the
    operator set up by hand."""

    def __init__(self, state_path: str, url: str = ACME_DNS_PUBLIC, log=print):
        self.url = url.rstrip("/")
        self.state_path = state_path
        self.log = log
        self.creds = self._load()

    def _load(self) -> dict | None:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def register(self) -> dict:
        """Claim a validation subdomain. Returns the credentials, and tells
        the operator the one CNAME they must create."""
        if self.creds:
            return self.creds
        creds, _ = _http(f"{self.url}/register", method="POST", payload={})
        for field in ("username", "password", "fulldomain", "subdomain"):
            if field not in creds:
                raise PublishError(f"{_host(self.url)} returned no {field} on register")
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        fd = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(creds, fh)
        self.creds = creds
        self.log(f"acme-dns registered at {_host(self.url)}; create this CNAME once:")
        self.log(f"  _acme-challenge.<your panel domain>.  CNAME  {creds['fulldomain']}.")
        return creds

    def publish(self, record: str, value: str) -> None:
        creds = self.register()
        _http(f"{self.url}/update", method="POST",
              headers={"X-Api-User": creds["username"], "X-Api-Key": creds["password"]},
              payload={"subdomain": creds["subdomain"], "txt": value},
              secret=creds["password"])

    def withdraw(self, record: str, value: str) -> None:
        # acme-dns keeps the last two values per subdomain and expires them
        # on its own; there is no delete endpoint, and nothing to clean up.
        return None


class CloudflarePublisher(_Base):
    """Cloudflare's API. Simplest to set up, widest blast radius: a token
    cannot be scoped to one record, so it can rewrite the whole zone."""

    API = "https://api.cloudflare.com/client/v4"

    def __init__(self, token: str, zone: str = "", log=print):
        if not token:
            raise PublishError("DUNE_ACME_CLOUDFLARE_TOKEN is empty")
        self.token = token
        self.zone_name = zone
        self.log = log
        self._zone_id: str | None = None
        self._created: dict[str, str] = {}

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _zone_id_for(self, record: str) -> str:
        if self._zone_id:
            return self._zone_id
        # Try the longest parent first: panel.example.com -> example.com.
        candidates = [self.zone_name] if self.zone_name else []
        labels = record.split(".")
        candidates += [".".join(labels[i:]) for i in range(len(labels) - 1)]
        for name in candidates:
            if not name:
                continue
            data, _ = _http(f"{self.API}/zones?name={urllib.parse.quote(name)}",
                            headers=self._auth(), secret=self.token)
            for zone in data.get("result") or []:
                self._zone_id = zone["id"]
                return self._zone_id
        raise PublishError(
            f"no Cloudflare zone found for {record}. Set DUNE_ACME_CLOUDFLARE_ZONE to the "
            "domain the token can see, and check the token has Zone:DNS:Edit on it.")

    def publish(self, record: str, value: str) -> None:
        zone_id = self._zone_id_for(record)
        data, _ = _http(f"{self.API}/zones/{zone_id}/dns_records", method="POST",
                        headers=self._auth(),
                        payload={"type": "TXT", "name": record, "content": value, "ttl": 60},
                        secret=self.token)
        result = data.get("result") or {}
        if not result.get("id"):
            raise PublishError(f"Cloudflare accepted the TXT for {record} but returned no id")
        self._created[value] = result["id"]

    def withdraw(self, record: str, value: str) -> None:
        record_id = self._created.pop(value, None)
        if not record_id or not self._zone_id:
            return
        _http(f"{self.API}/zones/{self._zone_id}/dns_records/{record_id}",
              method="DELETE", headers=self._auth(), secret=self.token)


def from_env(base: str, log=print):
    """Build the publisher the operator configured, or None if ACME is off."""
    backend = (os.environ.get("DUNE_ACME_DNS_BACKEND") or "").strip().lower()
    if not backend:
        return None
    if backend == "cloudflare":
        return CloudflarePublisher(
            (os.environ.get("DUNE_ACME_CLOUDFLARE_TOKEN") or "").strip(),
            (os.environ.get("DUNE_ACME_CLOUDFLARE_ZONE") or "").strip(), log=log)
    if backend == "acme-dns":
        return AcmeDnsPublisher(
            os.path.join(base, "server", "state", "tls", "acme-dns.json"),
            (os.environ.get("DUNE_ACME_DNS_URL") or ACME_DNS_PUBLIC).strip(), log=log)
    raise PublishError(
        f"DUNE_ACME_DNS_BACKEND={backend!r} is not one of: cloudflare, acme-dns")
