#!/usr/bin/env python3
"""ACME (Let's Encrypt) client for the admin panel, DNS-01 only.

Derived from acme-tiny by Daniel Roesler (MIT) —
https://github.com/diafygi/acme-tiny — whose ACME plumbing (JWS signing
through the openssl CLI, nonce handling, the order flow) is reused
faithfully. The challenge half is replaced: acme-tiny does HTTP-01, which
needs port 80, and a Pelican server is allocated high ports. DNS-01 needs
no inbound port at all. See ATTRIBUTION.md.

Why DNS-01 rather than HTTP-01:
  - HTTP-01 validates against port 80 of the domain. Panel allocations are
    arbitrary high ports; 80 is rarely allocatable. Dead end for most.
  - TLS-ALPN-01 has the same problem on 443.
  - DNS-01 proves control by publishing a TXT record, so the container
    needs no inbound connectivity whatsoever.

The credential that writes that TXT is the sensitive part, which is why
publishing is delegated to a backend (see admin_acme_dns) and why CNAME
delegation is documented as the preferred shape: Let's Encrypt follows
CNAMEs when looking up _acme-challenge, so the credential can be scoped to
a throwaway validation zone instead of the operator's real DNS.

No third-party Python packages: stdlib plus the openssl binary, both of
which the runtime image already has.
"""
from __future__ import annotations

import binascii
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
STAGING_DIRECTORY = "https://acme-staging-v02.api.letsencrypt.org/directory"
USER_AGENT = "pelican-egg-dune-awakening/acme (+https://github.com/Sergentval/pelican-egg-dune-awakening)"


class AcmeError(RuntimeError):
    """Anything that stops a certificate being issued. The message is shown
    to the operator, so it never carries the account key or a DNS token."""


def _b64(payload: bytes) -> str:
    """base64url without padding, as every ACME field requires."""
    import base64
    return base64.urlsafe_b64encode(payload).decode("utf8").replace("=", "")


def _openssl(args: list[str], stdin_bytes: bytes | None = None, what: str = "openssl") -> bytes:
    proc = subprocess.run(["openssl", *args], input=stdin_bytes,
                          capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise AcmeError(f"{what} failed: {proc.stderr.decode('utf8', 'replace')[:300]}")
    return proc.stdout


def generate_account_key(path: str) -> str:
    """A 4096-bit RSA account key, created once and reused. Kept 0600: it
    is what proves this installation owns its ACME account."""
    if os.path.isfile(path):
        return path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = _openssl(["genrsa", "4096"], what="account key generation")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return path


def generate_csr(key_path: str, domain: str, csr_path: str) -> str:
    """A CSR for one name. The server key is generated alongside it if
    absent — the certificate is worthless without the matching key."""
    if not os.path.isfile(key_path):
        os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
        key = _openssl(["genrsa", "2048"], what="server key generation")
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    csr = _openssl(["req", "-new", "-sha256", "-key", key_path,
                    "-subj", f"/CN={domain}",
                    "-addext", f"subjectAltName=DNS:{domain}"],
                   what="CSR generation")
    with open(csr_path, "wb") as fh:
        fh.write(csr)
    return csr_path


def account_thumbprint(account_key: str) -> tuple[dict, str]:
    """(JWK, thumbprint). Lifted from acme-tiny: parse the modulus and
    exponent out of `openssl rsa -text` because stdlib has no asymmetric
    crypto to read them with."""
    out = _openssl(["rsa", "-in", account_key, "-noout", "-text"],
                   what="reading the account key").decode("utf8")
    match = re.search(r"modulus:[\s]+?00:([a-f0-9\:\s]+?)\npublicExponent: ([0-9]+)",
                      out, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AcmeError(
            f"could not read an RSA public key out of {account_key} — is it an RSA key?")
    pub_hex, pub_exp = match.groups()
    pub_exp = f"{int(pub_exp):x}"
    pub_exp = f"0{pub_exp}" if len(pub_exp) % 2 else pub_exp
    jwk = {
        "e": _b64(binascii.unhexlify(pub_exp.encode("utf-8"))),
        "kty": "RSA",
        "n": _b64(binascii.unhexlify(re.sub(r"(\s|:)", "", pub_hex).encode("utf-8"))),
    }
    accountkey_json = json.dumps(jwk, sort_keys=True, separators=(",", ":"))
    return jwk, _b64(hashlib.sha256(accountkey_json.encode("utf8")).digest())


def txt_value(token: str, thumbprint: str) -> str:
    """The TXT record body for a dns-01 challenge: the base64url SHA-256 of
    the key authorization, NOT the key authorization itself. Publishing the
    latter is the classic dns-01 mistake — it validates as invalid with no
    useful error."""
    key_authorization = f"{token}.{thumbprint}"
    return _b64(hashlib.sha256(key_authorization.encode("utf8")).digest())


class _Session:
    """ACME transport: signed requests, nonce replay handling."""

    def __init__(self, account_key: str, directory_url: str, log=print):
        self.account_key = account_key
        self.log = log
        self.jwk, self.thumbprint = account_thumbprint(account_key)
        self.nonce: str | None = None
        self.kid: str | None = None
        self.directory = self._request(directory_url, err="fetching the ACME directory")[0]

    def _request(self, url, data=None, err="request failed", depth=0) -> tuple[Any, int, Any]:
        try:
            req = urllib.request.Request(url, data=data, headers={
                "Content-Type": "application/jose+json", "User-Agent": USER_AGENT})
            resp = urllib.request.urlopen(req, timeout=30)
            body, code, headers = resp.read().decode("utf8"), resp.getcode(), resp.headers
        except urllib.error.HTTPError as exc:
            body, code, headers = exc.read().decode("utf8"), exc.code, exc.headers
        except (urllib.error.URLError, OSError) as exc:
            raise AcmeError(f"{err}: {exc}") from None
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = body
        if headers.get("Replay-Nonce"):
            self.nonce = headers["Replay-Nonce"]
        if code not in (200, 201, 204) and depth < 100 and isinstance(parsed, dict) \
                and parsed.get("type") == "urn:ietf:params:acme:error:badNonce":
            return self._request(url, data, err, depth + 1)
        if code not in (200, 201, 204):
            detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
            raise AcmeError(f"{err}: HTTP {code} — {str(detail)[:300]}")
        return parsed, code, headers

    def signed(self, url, payload, err="request failed") -> tuple[Any, int, Any]:
        if self.nonce is None:
            self._request(self.directory["newNonce"], err="fetching a nonce")
        payload64 = "" if payload is None else _b64(json.dumps(payload).encode("utf8"))
        protected = {"url": url, "alg": "RS256", "nonce": self.nonce}
        protected.update({"jwk": self.jwk} if self.kid is None else {"kid": self.kid})
        protected64 = _b64(json.dumps(protected).encode("utf8"))
        signature = _openssl(["dgst", "-sha256", "-sign", self.account_key],
                             stdin_bytes=f"{protected64}.{payload64}".encode("utf8"),
                             what="signing an ACME request")
        body = json.dumps({"protected": protected64, "payload": payload64,
                           "signature": _b64(signature)}).encode("utf8")
        return self._request(url, body, err)

    def register(self, contact: str | None = None):
        payload = {"termsOfServiceAgreed": True}
        _account, code, headers = self.signed(
            self.directory["newAccount"], payload, "registering the ACME account")
        self.kid = headers["Location"]
        self.log(f"ACME account {'registered' if code == 201 else 'reused'}")
        if contact:
            self.signed(self.kid, {"contact": [f"mailto:{contact}"]},
                        "setting the ACME account contact")


def issue(domain: str, account_key: str, csr_path: str, publisher, *,
          directory_url: str = DEFAULT_DIRECTORY, contact: str | None = None,
          log=print, propagation_timeout: int = 300) -> str:
    """Run one DNS-01 order for `domain` and return the certificate chain.

    `publisher` publishes and withdraws the challenge TXT — see
    admin_acme_dns. It is always asked to withdraw, including on failure,
    so a stale _acme-challenge record is never left behind.
    """
    session = _Session(account_key, directory_url, log=log)
    session.register(contact)

    order, _, order_headers = session.signed(
        session.directory["newOrder"],
        {"identifiers": [{"type": "dns", "value": domain}]},
        f"creating an order for {domain}")

    published = []
    try:
        for auth_url in order["authorizations"]:
            authorization, _, _ = session.signed(auth_url, None, "reading the authorization")
            name = authorization["identifier"]["value"]
            if authorization["status"] == "valid":
                log(f"{name}: already authorised, skipping the challenge")
                continue
            try:
                challenge = [c for c in authorization["challenges"]
                             if c["type"] == "dns-01"][0]
            except IndexError:
                raise AcmeError(f"{name}: the CA offered no dns-01 challenge") from None

            record = f"_acme-challenge.{name}"
            value = txt_value(challenge["token"], session.thumbprint)
            log(f"{name}: publishing {record}")
            publisher.publish(record, value)
            published.append((record, value))

            if not publisher.wait_until_visible(record, value, timeout=propagation_timeout,
                                                log=log):
                raise AcmeError(
                    f"{record} did not become visible within {propagation_timeout}s. "
                    "Check the record was created, and that any CNAME delegation points "
                    "where the backend writes.")

            session.signed(challenge["url"], {}, f"submitting the challenge for {name}")
            deadline = time.time() + propagation_timeout
            while True:
                authorization, _, _ = session.signed(auth_url, None, "polling the authorization")
                if authorization["status"] != "pending":
                    break
                if time.time() > deadline:
                    raise AcmeError(f"{name}: the CA never finished validating")
                time.sleep(5)
            if authorization["status"] != "valid":
                problem = authorization.get("challenges", [{}])[0].get("error", {})
                raise AcmeError(f"{name}: validation failed — "
                                f"{problem.get('detail', authorization['status'])}")
            log(f"{name}: validated")

        csr_der = _openssl(["req", "-in", csr_path, "-outform", "DER"],
                           what="exporting the CSR")
        session.signed(order["finalize"], {"csr": _b64(csr_der)}, "finalising the order")

        deadline = time.time() + propagation_timeout
        while True:
            order, _, _ = session.signed(order_headers["Location"], None, "polling the order")
            if order["status"] in ("valid", "invalid"):
                break
            if time.time() > deadline:
                raise AcmeError("the CA never finished issuing")
            time.sleep(5)
        if order["status"] != "valid":
            raise AcmeError(f"the CA refused to issue: {order.get('error', order['status'])}")

        chain, _, _ = session.signed(order["certificate"], None, "downloading the certificate")
        log(f"certificate issued for {domain}")
        return chain if isinstance(chain, str) else json.dumps(chain)
    finally:
        for record, value in published:
            try:
                publisher.withdraw(record, value)
            except Exception as exc:  # noqa: BLE001 — cleanup must not mask the real error
                log(f"WARN could not withdraw {record}: {exc}")


def days_remaining(cert_path: str) -> int | None:
    """Days until the certificate expires, or None if it cannot be read.
    Drives renewal; None means "treat as needing attention", never "fine"."""
    try:
        out = _openssl(["x509", "-in", cert_path, "-noout", "-enddate"],
                       what="reading the certificate").decode("utf8")
    except (AcmeError, OSError):
        return None
    match = re.search(r"notAfter=(.+)", out)
    if not match:
        return None
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
        try:
            import datetime
            expiry = datetime.datetime.strptime(match.group(1).strip(), fmt).replace(
                tzinfo=datetime.timezone.utc)
            return int((expiry - datetime.datetime.now(datetime.timezone.utc)).days)
        except ValueError:
            continue
    return None
