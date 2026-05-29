#!/usr/bin/env python3
"""
FLS (Funcom Live Services) selective stub.

Listens on 127.0.0.1:443 with a self-signed cert for sb-retail.fls.funcom.com.
For /api/Battlegroups_IsPlayerAuthorized, returns a canned success response so
self-hosted players can complete cross-Sietch travel handshakes that real FLS
refuses with HTTP 500 "Invalid JSON string". Every other request is proxied
to real FLS so heartbeats, character data, and population reports keep working.

Wiring (handled by start-fls-stub.sh):

  /etc/hosts                      sb-retail.fls.funcom.com -> 127.0.0.1
  /etc/ssl/certs/ca-cert.crt      appended with our CA so UE5 trusts us
  $STATE/fls-stub/{cert,key}.pem  persisted between container restarts

Operational notes:

- Real FLS IP is resolved fresh at startup via getaddrinfo against the public
  DNS (1.1.1.1) since /etc/hosts pins the name to localhost. The resolved IP
  is cached in REAL_FLS_IP for the process lifetime. If FLS migrates, we
  rediscover on stub restart.
- Proxy uses the SNI of the original hostname and validates against the
  system CA store so we don't introduce a MITM hole on the upstream side.
- The stubbed response shape is a guess based on the observed wrapper used by
  all other Director-side FLS calls: {"data":{"FunctionResult":{...}}}. If
  UE5 rejects the shape, iterate on the FunctionResult payload — that is
  the only knob we have without binary symbols.
"""

import argparse
import http.server
import json
import logging
import socket
import ssl
import sys
from urllib.parse import urlsplit

REAL_FLS_HOSTNAME = "sb-retail.fls.funcom.com"
REAL_FLS_PORT = 443
PUBLIC_DNS = ("1.1.1.1", 53)

# Endpoints we stub. Keep narrow — only intercept what we have to.
#
# Each stub returns the {data:{FunctionResult:{...}}} wrapper that every
# successful FLS call we have observed uses. The inner fields are best
# guesses informed by the endpoint name; iterate as UE5 log errors surface
# specific missing fields (the C# binding errors give exact names).
STUBBED_PATHS = {
    "/api/Battlegroups_IsPlayerAuthorized",
    # Real FLS returns "Player not on authorization list" (153 bytes
    # observed) for self-host JWTs trying to cross-Sietch. We override
    # with a permissive empty list so the destination accepts the
    # transfer instead of rejecting it as "denied / not on the list".
    "/api/AccessControl_CheckPlayerAuthorizationList",
}


def _stub_payload(path: str) -> dict:
    """Return the canned success payload for a stubbed path. Field shapes
    are dispatched by endpoint name — each FLS endpoint expects different
    fields, and the C# bindings hard-fail on missing fields rather than
    treating them as falsy."""
    if path == "/api/Battlegroups_IsPlayerAuthorized":
        return {
            "data": {
                "FunctionResult": {
                    "IsPlayerAuthorized": True,
                    "IsAuthorized": True,
                    "Authorized": True,
                    "Result": "Authorized",
                    "ErrorCode": 0,
                    "ErrorMessage": "",
                }
            }
        }
    if path == "/api/AccessControl_CheckPlayerAuthorizationList":
        # Permissive: empty denial list, all checks passed.
        return {
            "data": {
                "FunctionResult": {
                    "AuthorizationList": [],
                    "DeniedPlayers": [],
                    "BlockedPlayers": [],
                    "IsAuthorized": True,
                    "Result": "Allowed",
                    "ErrorCode": 0,
                    "ErrorMessage": "",
                }
            }
        }
    # Defensive default — won't normally hit because we only stub the
    # paths we explicitly enumerate, but keeps the type checker happy.
    return {"data": {"FunctionResult": {}}}


def resolve_real_fls_ip() -> str:
    """Resolve real FLS IP bypassing /etc/hosts (which now points to us).

    Sends a one-shot UDP DNS query directly to 1.1.1.1 so we don't trip the
    self-rewrite. Falls back to the published IP (150.171.110.117) if the
    direct query fails.
    """
    try:
        # Build minimal DNS query packet.
        qname = b""
        for label in REAL_FLS_HOSTNAME.split("."):
            qname += bytes([len(label)]) + label.encode("ascii")
        qname += b"\x00"
        query = (
            b"\x12\x34"  # transaction id (arbitrary)
            b"\x01\x00"  # flags: standard query, recursion desired
            b"\x00\x01\x00\x00\x00\x00\x00\x00"  # qcount=1, ancount/nscount/arcount=0
            + qname
            + b"\x00\x01"  # qtype=A
            b"\x00\x01"  # qclass=IN
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        sock.sendto(query, PUBLIC_DNS)
        resp, _ = sock.recvfrom(2048)
        sock.close()

        # Find the first A record in the answer section. Crude: scan for the
        # ANCOUNT, then walk past the question and read the first RDATA.
        ancount = int.from_bytes(resp[6:8], "big")
        if ancount == 0:
            raise RuntimeError("no A records in DNS response")
        # Skip header (12 bytes) + question (qname + 4 bytes for type+class).
        # Reuse our outgoing qname length since it matches what came back.
        idx = 12 + len(qname) + 4
        # Walk answer RRs until we find a type-A (1) record.
        for _ in range(ancount):
            # Name field: either pointer (0xc0 byte then 1 more byte) or label seq.
            if resp[idx] & 0xC0 == 0xC0:
                idx += 2
            else:
                while resp[idx] != 0:
                    idx += resp[idx] + 1
                idx += 1
            rtype = int.from_bytes(resp[idx : idx + 2], "big")
            rdlength = int.from_bytes(resp[idx + 8 : idx + 10], "big")
            rdata_start = idx + 10
            if rtype == 1 and rdlength == 4:
                octets = resp[rdata_start : rdata_start + 4]
                return ".".join(str(b) for b in octets)
            idx = rdata_start + rdlength
        raise RuntimeError("no A record returned")
    except Exception as e:
        logging.warning("DNS query failed (%s), falling back to hardcoded IP", e)
        return "150.171.110.117"


class FlsStubHandler(http.server.BaseHTTPRequestHandler):
    real_fls_ip = "150.171.110.117"  # overwritten in main()

    # Logging: route through our logger so it goes to stdout with our format.
    def log_message(self, format: str, *args) -> None:  # noqa: N802, A002
        logging.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        body_len = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(body_len) if body_len else b""

        if path in STUBBED_PATHS:
            self._serve_stub(path, body)
            return
        self._proxy_upstream(body)

    def _serve_stub(self, path: str, body: bytes) -> None:
        """Return the canned success response."""
        logging.info(
            "STUB %s code_query=%s body_bytes=%d",
            path,
            "yes" if "code=" in self.path else "no",
            len(body),
        )
        payload = _stub_payload(path)
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _proxy_upstream(self, body: bytes) -> None:
        """Forward request to real FLS, return response verbatim.

        We connect TCP to the resolved real-FLS IP (since /etc/hosts now
        points the hostname at us in some setups, and DNS lookups via
        socket.create_connection on the hostname would either loop back to
        us or fail). The TLS handshake passes server_hostname set to the
        FLS hostname so the cert's CN/SAN check passes.

        We build the request manually instead of going through
        http.client.HTTPSConnection — that class derives server_hostname
        from self.host, so any way of pointing it at the IP also breaks
        the TLS check. Doing it by hand keeps the two cleanly separated.
        """
        ctx = ssl.create_default_context()
        sock = None
        try:
            sock = socket.create_connection((self.real_fls_ip, REAL_FLS_PORT), timeout=15)
            tls = ctx.wrap_socket(sock, server_hostname=REAL_FLS_HOSTNAME)

            # Build HTTP/1.1 request manually. Path keeps the original
            # query string (the JWT in `code=...`). Hop-by-hop headers
            # dropped so a fresh keep-alive negotiation happens upstream.
            req_lines = [f"{self.command} {self.path} HTTP/1.1", f"Host: {REAL_FLS_HOSTNAME}"]
            for k, v in self.headers.items():
                lk = k.lower()
                if lk in ("host", "content-length", "transfer-encoding", "connection"):
                    continue
                req_lines.append(f"{k}: {v}")
            if body:
                req_lines.append(f"Content-Length: {len(body)}")
            req_lines.append("Connection: close")
            req_lines.append("")
            req_lines.append("")
            tls.sendall("\r\n".join(req_lines).encode("iso-8859-1"))
            if body:
                tls.sendall(body)

            # Read the entire response into memory. Connection: close
            # upstream means we drain until EOF.
            chunks: list[bytes] = []
            while True:
                buf = tls.recv(8192)
                if not buf:
                    break
                chunks.append(buf)
            raw = b"".join(chunks)
            head, sep, raw_body = raw.partition(b"\r\n\r\n")
            if not sep:
                raise RuntimeError("malformed upstream response (no header/body separator)")
            status_line, _, header_block = head.partition(b"\r\n")
            parts = status_line.split(b" ", 2)
            if len(parts) < 2:
                raise RuntimeError(f"bad status line: {status_line!r}")
            status = int(parts[1])

            # Parse headers + detect chunked transfer-encoding. FLS returns
            # chunked for any payload > a few hundred bytes, so we have to
            # decode it before forwarding — otherwise the client sees the
            # chunk size markers as part of the body.
            upstream_headers: list[tuple[str, str]] = []
            is_chunked = False
            for header in header_block.split(b"\r\n"):
                k, _, v = header.partition(b": ")
                if not v:
                    continue
                key = k.decode("iso-8859-1")
                val = v.decode("iso-8859-1")
                kl = key.lower()
                if kl == "transfer-encoding" and "chunked" in val.lower():
                    is_chunked = True
                # Skip hop-by-hop + framing — http.server reframes for us.
                if kl in ("transfer-encoding", "connection", "content-length"):
                    continue
                upstream_headers.append((key, val))

            body_bytes = _dechunk(raw_body) if is_chunked else raw_body
            logging.info(
                "PROXY %s %s -> %d (%d bytes%s)",
                self.command,
                self.path[:80],
                status,
                len(body_bytes),
                " dechunked" if is_chunked else "",
            )
            self.send_response(status)
            for key, val in upstream_headers:
                self.send_header(key, val)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        except Exception as e:  # noqa: BLE001 — log + return generic upstream error
            logging.exception("upstream proxy failed: %s", e)
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"upstream error: {e}".encode("utf-8"))
            return
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass


def _dechunk(raw: bytes) -> bytes:
    """Decode RFC 7230 chunked transfer-encoding into a plain bytestring.

    Real FLS responds chunked for payloads larger than a few hundred
    bytes. The chunk size line is hex digits (optionally followed by
    extension parameters after a `;`), terminated by CRLF; the chunk
    body follows, then another CRLF, repeated until a zero-length chunk
    marks the end. Trailing headers after the final chunk are discarded.
    """
    out = bytearray()
    pos = 0
    while pos < len(raw):
        eol = raw.find(b"\r\n", pos)
        if eol < 0:
            break
        size_line = raw[pos:eol].split(b";", 1)[0].strip()
        if not size_line:
            pos = eol + 2
            continue
        try:
            size = int(size_line, 16)
        except ValueError:
            break
        pos = eol + 2
        if size == 0:
            break
        out.extend(raw[pos : pos + size])
        pos += size + 2  # skip chunk body + trailing CRLF
    return bytes(out)


class ThreadedHTTPSServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert", help="TLS cert PEM path (omit for plaintext)")
    parser.add_argument("--key", help="TLS key PEM path (omit for plaintext)")
    parser.add_argument("--bind", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8443, help="bind port")
    parser.add_argument(
        "--plaintext",
        action="store_true",
        help="Serve plain HTTP instead of HTTPS. UE5 is statically linked "
        "with libcurl hardcoded to verify peers, so we can't make it "
        "trust a self-signed cert. Plaintext loopback bypasses the "
        "trust problem; the stub still proxies upstream over verified "
        "TLS, so credentials never travel in plaintext over the wire.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[fls-stub] [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    real_ip = resolve_real_fls_ip()
    FlsStubHandler.real_fls_ip = real_ip
    logging.info("Real FLS IP resolved to %s", real_ip)

    if args.plaintext:
        srv = ThreadedHTTPSServer((args.bind, args.port), FlsStubHandler)
        scheme = "http"
    else:
        if not args.cert or not args.key:
            logging.error("--cert and --key required without --plaintext")
            return 1
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)
        srv = ThreadedHTTPSServer((args.bind, args.port), FlsStubHandler)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    logging.info(
        "Listening on %s://%s:%d (stubs: %s)",
        scheme,
        args.bind,
        args.port,
        ", ".join(sorted(STUBBED_PATHS)),
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutdown")
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
