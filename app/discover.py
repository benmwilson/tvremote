"""Finding the TVs on the network, and guessing which Roku goes with which screen.
Rokus answer an SSDP search (UDP multicast, `roku:ecp`); Chromecasts are already found by the Chromecast target (mDNS); the Pi's own
screen is a connected HDMI output. The guess is only a starting point: the wizard shows why and the person can change any of it."""
import concurrent.futures
import glob
import re
import socket
import time

SSDP = ("239.255.255.250", 1900)
SEARCH = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: roku:ecp\r\n\r\n").encode()


def parse_ssdp(text):
    """The Roku's address from an SSDP answer, or None when the answer isn't a Roku's."""
    if not re.search(r"^ST:\s*roku:ecp", text, re.I | re.M):
        return None
    m = re.search(r"^LOCATION:\s*https?://([\d.]+)", text, re.I | re.M)
    return m.group(1) if m else None


def roku_scan(timeout=2.5):
    """Addresses of the Rokus that answer within `timeout` seconds."""
    found = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.settimeout(0.4)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        for _ in range(2):  # UDP: ask twice
            s.sendto(SEARCH, SSDP)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            ip = parse_ssdp(data.decode(errors="replace"))
            if ip and ip not in found:
                found.append(ip)
    except OSError:
        pass  # no network to search on
    finally:
        s.close()
    return found


def describe(ips, info):
    """Each Roku's details, asked in parallel. A TV that won't answer still appears, with an error."""
    def one(ip):
        try:
            return {"ip": ip, **{k: v for k, v in info(ip).items() if k in ("name", "brand", "model", "size", "app_id")}}
        except Exception as e:
            return {"ip": ip, "error": str(e)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        return list(ex.map(one, ips))


def display_connected():
    """Is a screen plugged into one of the Pi's HDMI ports?"""
    for path in glob.glob("/sys/class/drm/card*-HDMI-A-*/status"):
        try:
            with open(path) as f:
                if f.read().strip() == "connected":
                    return True
        except OSError:
            pass
    return False


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def guess(rokus, casts, display, cec_name, cec_phys):
    """{"living": {...}, "bedroom": {...}}: a first guess for each slot, with `why` sentences. Nothing is claimed that isn't evidenced:
    an unclear pairing is left empty and says so."""
    pool = list(rokus)
    living = {"enabled": bool(display), "roku": "", "input": "", "why": []}
    if display:
        match = next((r for r in pool if _norm(cec_name) and _norm(r.get("model")) == _norm(cec_name)), None)
        if match:
            living["roku"] = match["ip"]
            living["why"].append(f"The TV calls itself “{cec_name}” over HDMI, the same as this Roku's model.")
            pool.remove(match)
        elif cec_name:
            living["why"].append(f"The TV on the Pi's HDMI calls itself “{cec_name}”, which matches none of the Rokus found. Pick its Roku, if it has one.")
        port = (cec_phys or "").split(".")[0]
        if port.isdigit() and 1 <= int(port) <= 4:
            living["input"] = f"HDMI{port}"
            living["why"].append(f"The Pi is on HDMI {port} of the TV (from its HDMI address).")
    else:
        living["why"].append("No screen is connected to the Pi, so this slot is off.")

    bedroom = {"enabled": bool(casts), "roku": "", "input": "", "device": casts[0]["name"] if casts else None, "why": []}
    if casts:
        if len(pool) == 1:
            bedroom["roku"] = pool[0]["ip"]
            bedroom["why"].append("It is the only Roku left once the Pi's TV is taken, so it is probably the one the Chromecast is plugged into.")
            m = re.fullmatch(r"tvinput\.hdmi(\d)", pool[0].get("app_id") or "")
            if m:
                bedroom["input"] = f"HDMI{m.group(1)}"
                bedroom["why"].append(f"That TV is showing HDMI {m.group(1)} right now.")
        elif len(pool) > 1:
            bedroom["why"].append("Several Rokus could be the Chromecast's TV: pick one.")
        else:
            bedroom["why"].append("No Roku is left to go with the Chromecast. It still works without TV controls.")
    else:
        bedroom["why"].append("No Chromecast was found. Check it is on the same network.")
    return {"living": living, "bedroom": bedroom}
