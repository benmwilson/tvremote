"""One command over an mpv IPC socket (shared by the stream player and the home screen)."""
import json
import socket


def send(sock_path, cmd, timeout=1.0):
    """Send one command to the mpv listening on sock_path and return its reply, or None if mpv isn't there."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock_path)
            s.sendall((json.dumps({"command": cmd, "request_id": 1}) + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return None
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("request_id") == 1:
                        return msg
    except (OSError, AttributeError):  # AttributeError: no AF_UNIX (Windows test machines)
        return None


def set_mute(sock_path, muted):
    """Mute or unmute one player. Unmuting mutes and unmutes again: the sound server (WirePlumber) remembers a stream's mute and puts it back on the next mpv,
    so a player that starts silent reports "not muted" itself, and setting the same value again would change nothing."""
    if not muted:
        send(sock_path, ["set_property", "mute", True])
    return send(sock_path, ["set_property", "mute", bool(muted)])

