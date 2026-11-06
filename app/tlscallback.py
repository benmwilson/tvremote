"""A tiny https listener whose only job is Kick's login redirect (Kick accepts https redirect URLs only).
It forwards the one-time code to the normal http app on the same Pi, so the code never leaves the LAN.
The certificate is self-signed for the redirect host; the phone shows a warning once per login.
Each connection does its TLS handshake in its own thread with a 10 s timeout."""
import http.server
import os
import ssl
import subprocess
from urllib.parse import parse_qs, urlencode, urlparse

PATHS = {"/auth/kick/callback": "kick", "/auth/twitch/callback": "twitch"}  # which site's login a redirect belongs to


def ensure_cert(data_dir, host):
    cert, key = os.path.join(data_dir, "tls.crt"), os.path.join(data_dir, "tls.key")
    if not (os.path.exists(cert) and os.path.exists(key)):
        san = ("IP:" if host.replace(".", "").isdigit() else "DNS:") + host
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-subj", "/CN=tvremote",
                        "-addext", "subjectAltName=" + san, "-keyout", key, "-out", cert],
                       check=True, capture_output=True)
        os.chmod(key, 0o600)
    return cert, key


def make_server(redirect, http_port, data_dir):
    """redirect: the registered https URL (host and port come from it); returns the server, not yet serving."""
    u = urlparse(redirect)
    base = f"http://{u.hostname}:{http_port}/auth/"

    class H(http.server.BaseHTTPRequestHandler):
        timeout = 10

        def setup(self):
            self.request.settimeout(self.timeout)
            self.request.do_handshake()  # in this connection's own thread, so a silent client can't hold up the next one
            super().setup()

        def do_GET(self):
            p = urlparse(self.path)
            if p.path not in PATHS:
                self.send_error(404)
                return
            q = parse_qs(p.query)
            keep = {k: q[k][0] for k in ("code", "state", "error") if k in q}  # nothing else is forwarded
            self.send_response(303)
            self.send_header("Location", f"{base}{PATHS[p.path]}/done?" + urlencode(keep))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 443 if u.port is None else u.port), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*ensure_cert(data_dir, u.hostname))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    srv.handle_error = lambda *a: None  # failed handshakes (scanners, a phone refusing the certificate) are not worth a traceback
    return srv
