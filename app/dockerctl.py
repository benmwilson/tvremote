"""Docker controls from inside the container, through the engine's socket (/var/run/docker.sock mounted into it).
Only list, inspect, logs and start / stop / restart / kill: nothing that creates, execs into or removes a container.
Anyone who can use the socket has root on the host, so the routes in app.py are admin-only and the whole thing is off when the socket isn't mounted."""
import http.client
import json
import os
import re
import socket
import struct

SOCK = os.environ.get("TVREMOTE_DOCKER_SOCK", "/var/run/docker.sock")
SELF = os.environ.get("TVREMOTE_CONTAINER", "tvremote")  # this app's own container name (compose container_name)
ACTIONS = ("start", "stop", "restart", "kill")
SECRET = re.compile(r"SECRET|TOKEN|PASS|KEY|CODE|COOKIE|CREDENTIAL", re.I)  # environment variables whose values are never sent to the phone
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
MAX_LOG = 200_000  # characters of log sent to the phone


class _Conn(http.client.HTTPConnection):
    def __init__(self, timeout):
        super().__init__("docker", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCK)


def _call(method, path, timeout=15):  # one place to stub in tests
    c = _Conn(timeout)
    try:
        c.request(method, path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def available():
    return os.path.exists(SOCK)


def containers():
    """Every container on the host: name, image, state, status line, and whether it is this app."""
    status, body = _call("GET", "/containers/json?all=1")
    if status != 200:
        raise OSError(f"Docker answered {status}")
    rows = [{"name": (c.get("Names") or ["?"])[0].lstrip("/"), "image": c.get("Image"), "state": c.get("State"), "status": c.get("Status")}
            for c in json.loads(body)]
    for r in rows:
        r["self"] = r["name"] == SELF
    return sorted(rows, key=lambda r: (not r["self"], r["name"]))


def _known(name):
    if not NAME.fullmatch(name or "") or name not in {c["name"] for c in containers()}:
        raise ValueError("Docker doesn't have a container with that name.")
    return name


USERINFO = re.compile(r"(://[^/\s:@]+):[^@\s/]+@")  # a password inside an address: scheme://user:password@host


def scrub(text):
    return USERINFO.sub(lambda m: m.group(1) + ":****@", text or "")


def redact(env, show_values=True):
    """['A=1', 'API_TOKEN=x'] -> [('A', '1'), ('API_TOKEN', None)]: the value of anything secret-looking is dropped, and with
    show_values False (any container but this app) every value is, since a name list can't catch every secret."""
    out = []
    for e in env or []:
        k, _, v = e.partition("=")
        out.append((k, None if SECRET.search(k) or not show_values else scrub(v)))
    return out


def detail(name):
    """The settings of one container as the phone shows them. Environment values that look secret are left out."""
    _known(name)
    status, body = _call("GET", f"/containers/{name}/json")
    if status != 200:
        raise OSError(f"Docker answered {status}")
    j = json.loads(body)
    st, cfg, host = j.get("State") or {}, j.get("Config") or {}, j.get("HostConfig") or {}
    labels = cfg.get("Labels") or {}
    return {
        "name": name, "id": (j.get("Id") or "")[:12], "image": cfg.get("Image"), "created": j.get("Created"),
        "state": st.get("Status"), "started": st.get("StartedAt"), "finished": st.get("FinishedAt"), "exit_code": st.get("ExitCode"),
        "oom": bool(st.get("OOMKilled")), "restarts": j.get("RestartCount"), "health": (st.get("Health") or {}).get("Status"),
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "no", "network": host.get("NetworkMode"),
        "user": cfg.get("User") or "root", "privileged": bool(host.get("Privileged")),
        "memory_limit": host.get("Memory") or None, "group_add": host.get("GroupAdd") or [],
        "devices": [d.get("PathOnHost") for d in host.get("Devices") or []],
        "mounts": [{"from": m.get("Source") or m.get("Name"), "to": m.get("Destination"), "rw": bool(m.get("RW")), "type": m.get("Type")} for m in j.get("Mounts") or []],
        "ports": sorted({f"{b.get('HostPort')}->{p}" for p, bs in (host.get("PortBindings") or {}).items() for b in bs or []}),
        "command": scrub(" ".join((cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or []))),
        "env": redact(cfg.get("Env"), name == SELF),
        "compose": {"project": labels.get("com.docker.compose.project"), "service": labels.get("com.docker.compose.service"),
                    "files": labels.get("com.docker.compose.project.config_files")},
    }


def _demux(raw):
    """The engine multiplexes stdout and stderr of a container without a terminal as frames: 1 byte stream, 3 spare, 4 bytes size, data."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out)


def logs(name, tail=200):
    _known(name)
    tail = max(1, min(int(tail), 2000))
    status, raw = _call("GET", f"/containers/{name}/logs?stdout=1&stderr=1&tail={tail}&timestamps=1", timeout=20)
    if status != 200:
        raise OSError(f"Docker answered {status}")
    tty = False
    try:
        s2, b2 = _call("GET", f"/containers/{name}/json")
        tty = bool(json.loads(b2).get("Config", {}).get("Tty")) if s2 == 200 else False
    except Exception:
        pass
    text = (raw if tty else _demux(raw)).decode(errors="replace")
    return text[-MAX_LOG:]


def act(name, action):
    """start / stop / restart / kill. Answers a sentence for the phone."""
    if action not in ACTIONS:
        raise ValueError("Not an action.")
    _known(name)
    q = "?t=5" if action in ("stop", "restart") else ""  # five seconds to shut down before it is killed
    status, body = _call("POST", f"/containers/{name}/{action}{q}", timeout=30)
    if status in (204, 200):
        return {"start": "Started.", "stop": "Stopped.", "restart": "Restarted.", "kill": "Killed."}[action]
    if status == 304:
        return "It was already " + ("running." if action == "start" else "stopped.")
    try:
        msg = json.loads(body).get("message")
    except ValueError:
        msg = None
    raise OSError(msg or f"Docker answered {status}")


if __name__ == "__main__":
    import sys
    sys.exit("run the tests: python -m unittest tests.test_dockerctl")
