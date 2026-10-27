"""Deployment preflight and bounded HTTPS verification. No DNS record writes."""
import http.client
import ipaddress
import json
import multiprocessing
import re
import ssl
import time

from cloudflare_dns import SyncError, hostnames


def published_port(model, service="proxy"):
    """Read the resolved Compose model without displaying its environment/secrets."""
    try:
        ports = model["services"][service]["ports"]
        if len(ports) != 1:
            raise ValueError
        port = ports[0]
        if port["target"] != 443 or port.get("protocol", "tcp") != "tcp":
            raise ValueError
        published = str(port["published"])
        if not re.fullmatch(r"[0-9]{1,5}", published) or not 1 <= int(published) <= 65535:
            raise ValueError
        address = ipaddress.ip_address(port["host_ip"])
        if address.is_multicast or getattr(address, "scope_id", None):
            raise ValueError
        return str(address), int(published)
    except (KeyError, TypeError, ValueError, IndexError):
        raise SyncError("Configure exactly one proxy port: TCP target 443, an explicit host IP, and CADDY_HTTPS_PORT from 1 to 65535.") from None


def compose_input(stream):
    try:
        return json.load(stream)
    except (ValueError, OSError):
        raise SyncError("Supply resolved Compose JSON on stdin; use the supplied Just recipe. Do not print it: it contains credentials.") from None


def preflight(model, running, service="proxy"):
    address, port = published_port(model, service)
    try:
        # Compose versions emit either an array or one JSON object per line.
        rows = []
        for line in running.splitlines():
            value = json.loads(line)
            rows.extend(value if isinstance(value, list) else [value])
        if len(rows) > 1:
            raise ValueError
        for row in rows:
            if row["Service"] != service or row["State"] != "running":
                raise ValueError
            publishers = row.get("Publishers") or []
            for binding in publishers:
                if (binding["TargetPort"] == 443 and binding["Protocol"] == "tcp"
                        and binding["PublishedPort"] == port
                        and ipaddress.ip_address(binding["URL"]) == ipaddress.ip_address(address)):
                    return "reuse"
    except (KeyError, TypeError, ValueError):
        raise SyncError("Cannot identify the running proxy's port bindings; use one proxy replica and a supported Docker Compose version.") from None
    return "probe"


def verification_config(env, port):
    names = hostnames(env.get("PROXY_HOSTNAMES", ""))
    path = env.get("PROXY_VERIFY_PATH", "/")
    if (not path.startswith("/") or path.startswith("//") or "#" in path
            or not path.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in path)):
        raise SyncError("PROXY_VERIFY_PATH must be an ASCII absolute path, optionally with a query; no spaces or fragments.")
    try:
        status = int(env.get("PROXY_VERIFY_STATUS", "200"))
        timeout = int(env.get("PROXY_VERIFY_TIMEOUT", "120"))
        if not 100 <= status <= 599 or not 1 <= timeout <= 600 or not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SyncError("Use PROXY_VERIFY_STATUS 100–599, PROXY_VERIFY_TIMEOUT 1–600 seconds, and an HTTPS port 1–65535.") from None
    body = env.get("PROXY_VERIFY_BODY", "").encode("utf-8")
    if len(body) > 65536:
        raise SyncError("PROXY_VERIFY_BODY must be at most 65536 UTF-8 bytes.")
    return names, path, status, body, timeout


def request_result(name, port, path, status, body):
    """Check system trust and hostname; never follow redirects or print responses."""
    connection = None
    try:
        connection = http.client.HTTPSConnection(name, port, timeout=5, context=ssl.create_default_context())
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != status:
            return f"HTTP {response.status}; expected {status}"
        if body and body not in response.read(65536):
            return "expected body marker absent from the first 65536 bytes"
        return None
    except ssl.SSLCertVerificationError:
        return "certificate trust, hostname, or expiry check failed"
    except ssl.SSLError:
        return "TLS handshake failed"
    except (OSError, http.client.HTTPException):
        return "DNS, connection, or response failed"
    finally:
        if connection is not None:
            connection.close()


def _request_worker(sender, args):
    try:
        sender.send(request_result(*args))
    finally:
        sender.close()


def bounded_request(args, timeout):
    # Socket timeouts do not bound DNS resolution or slow response streams.
    # A disposable child lets the parent enforce the entire attempt's deadline.
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_request_worker, args=(sender, args))
    try:
        process.start()
        sender.close()
        process.join(timeout)
        if process.is_alive():
            return "DNS, TLS, or response attempt timed out"
        if process.exitcode != 0 or not receiver.poll():
            return "verification worker failed"
        return receiver.recv()
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        receiver.close()
        sender.close()
        process.close()


def verify(env, port):
    names, path, status, body, timeout = verification_config(env, port)
    deadline = time.monotonic() + timeout
    pending = dict.fromkeys(names, "not checked before the deadline")
    while pending and time.monotonic() < deadline:
        for name in list(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            error = bounded_request((name, port, path, status, body), min(5, remaining))
            if error is None:
                print(f"Verified https://{name}:{port}{path} - trusted TLS, HTTP {status}" + (", body marker matched" if body else ""), flush=True)
                del pending[name]
            else:
                pending[name] = error
        if pending:
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    if pending:
        for name, error in pending.items():
            print(f"Not verified: {name}: {error}", flush=True)
        raise SyncError(f"HTTPS verification did not pass within {timeout} seconds. Check client DNS/routing, Caddy logs, and the application's expected response.")
    print("Verified from this client container. Repeat from the intended client network; this does not prove other clients can connect.")
