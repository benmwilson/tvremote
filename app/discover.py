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


def parse_phys(text):
    """The Pi's HDMI address (like 2.0.0.0) from `cec-ctl` output, or None. The first number is the TV's HDMI port the Pi is plugged into."""
    m = re.search(r"Physical Address\s*:\s*(\d+(?:\.\d+){3})", text or "")
    return m.group(1) if m and m.group(1) != "f.f.f.f" else None


def phys_input(phys):
    """'HDMI2' for '2.0.0.0'; '' when the address doesn't name a port (1 to 4)."""
    port = (phys or "").split(".")[0]
    return f"HDMI{port}" if port in ("1", "2", "3", "4") else ""


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def suggest(rokus, casts, display, cec_name, pi_input, saved):
    """One card per TV for the wizard: every Roku found (a saved one keeps its saved name and what is plugged in), then every saved TV the
    scan didn't find. A Roku that isn't saved yet gets a first guess for the Pi and the Chromecast, with `why` sentences; nothing is
    guessed once a TV already holds the Pi or the Chromecast. `pi_detected` is the HDMI input the Pi really is on, for the TV that holds it."""
    saved = [dict(t) for t in saved or []]
    by_ip = {t["roku"]: t for t in saved if t.get("roku")}
    have_pi, have_cc = any(t.get("pi") for t in saved), any(t.get("chromecast") for t in saved)
    out, fresh, used = [], [], set()

    def card(t, r, found, why):
        pi = t.get("pi")
        return {"id": t.get("id") or "", "name": t.get("name") or (r or {}).get("name") or "TV", "roku": t.get("roku") or (r or {}).get("ip") or "",
                "found": found, "info": r, "pi": pi, "chromecast": t.get("chromecast"), "why": why,
                "pi_detected": pi_input if pi and display else ""}

    for r in rokus:
        t = by_ip.get(r["ip"])
        if t:
            used.add(t["id"])
            why = []
            if t.get("pi") and not display:
                why.append("No screen is attached to the Pi right now. Plug it in, or untick the Pi here.")
            out.append(card(t, r, True, why))
        else:
            fresh.append(r)

    missing = [t for t in saved if t["id"] not in used]
    pick = None
    if display and not have_pi:
        pick = next((r for r in fresh if _norm(cec_name) and _norm(r.get("model")) == _norm(cec_name)), None) or (fresh[0] if len(fresh) == 1 else None)
    cc_pick = None
    if casts and not have_cc:
        left = [r for r in fresh if r is not pick]
        cc_pick = left[0] if len(left) == 1 else (pick if pick and len(fresh) == 1 else None)
    for r in fresh:
        why, t = [], {"name": (r.get("name") or "TV")[:24], "roku": r["ip"], "pi": None, "chromecast": None}
        if r is pick:
            t["pi"] = {"input": pi_input}
            why.append(f"The Pi is on HDMI {pi_input[-1]} of this TV (from its HDMI address)." if pi_input else "The Pi's screen is plugged into this TV.")
        if r is cc_pick:
            m = re.fullmatch(r"tvinput\.hdmi(\d)", r.get("app_id") or "")
            t["chromecast"] = {"device": casts[0]["name"], "input": f"HDMI{m.group(1)}" if m else ""}
            why.append("The Chromecast is probably on this TV." + (f" It is showing HDMI {m.group(1)} right now." if m else ""))
        if not display and not why:
            why.append("No screen is attached to the Pi, so it can't be plugged into a TV.")
        elif display and not have_pi and pick is None and len(fresh) > 1:
            why.append("Couldn't tell which TV the Pi is on" + (f" ({cec_name} matches none)" if cec_name else " (a TV that is off doesn't say)")
                       + ". Tick it on the right TV, or turn that TV on and Scan again.")
        if missing:
            why.append("A saved TV didn't answer. If this is the same TV with a new address, move its settings here and remove the old one.")
        out.append(card(t, r, True, why))
    for t in missing:
        out.append(card(t, None, not t.get("roku"), []))  # a TV with no Roku set (a Chromecast alone) isn't "didn't answer"
    return out
