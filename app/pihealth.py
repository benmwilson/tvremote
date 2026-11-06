"""The Pi's own health for the Debug page and the heat guard: temperature, CPU, memory, throttling, per-player load.
Plain reads of /sys and /proc (no extra packages, no vcgencmd), cheap enough to call every few seconds."""
import os
import time

WARM, HOT, COOL = 78.0, 82.0, 74.0  # deg C: show a warning, lower the pictures, and the level where it counts as cooled again
THROTTLE_BITS = {0: "under-voltage", 1: "arm frequency capped", 2: "throttled", 3: "soft temperature limit",
                 16: "under-voltage since boot", 17: "frequency capped since boot", 18: "throttled since boot", 19: "soft temperature limit since boot"}
_THROTTLE_FILES = ("/sys/devices/platform/soc/soc:firmware/get_throttled", "/sys/class/hwmon/hwmon0/device/get_throttled")
_last = {}


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def temperature():
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(raw) / 1000, 1)
    except (TypeError, ValueError):
        return None


def level(temp):
    """ok / warm / hot, from the temperature alone (the guard adds the cool-down gap itself)."""
    return "unknown" if temp is None else "hot" if temp >= HOT else "warm" if temp >= WARM else "ok"


def throttled():
    """Names of the Pi's problem flags that are set (empty when fine, None when the kernel does not say). This kernel has no get_throttled
    file, so it uses what it does show: the under-voltage alarm (hwmon rpi_volt) and a CPU clock below its maximum."""
    flags, known = [], False
    for p in _THROTTLE_FILES:  # older kernels
        raw = _read(p)
        if raw:
            try:
                v = int(raw.strip(), 16)
                return [name for bit, name in THROTTLE_BITS.items() if v >> bit & 1]
            except ValueError:
                pass
    for d in ("/sys/class/hwmon/" + n for n in (os.listdir("/sys/class/hwmon") if os.path.isdir("/sys/class/hwmon") else [])):
        if (_read(d + "/name") or "").strip() == "rpi_volt":
            known = True
            if (_read(d + "/in0_lcrit_alarm") or "0").strip() == "1":
                flags.append("under-voltage")
    cur, top = cpu_mhz(), _read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    if cur is not None and top and cur < int(top) // 1000 - 50 and (_read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or "").strip() != "ondemand":
        flags.append("CPU clock lowered")  # (with the ondemand governor a low clock just means idle)
    return flags if known or cur is not None else None


def cpu_mhz():
    raw = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    return int(raw) // 1000 if raw and raw.strip().isdigit() else None


def _cpu_times():
    line = (_read("/proc/stat") or "").splitlines()[:1]
    parts = line[0].split()[1:] if line else []
    nums = [int(x) for x in parts[:8]] if len(parts) >= 4 else []
    return (sum(nums) - nums[3] - (nums[4] if len(nums) > 4 else 0), sum(nums)) if nums else None  # (busy, total); idle and iowait are not busy


def meminfo():
    out = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if v.strip().endswith("kB"):
            out[k] = int(v.split()[0]) // 1024
    return out


def players():
    """CPU % (of one core) and memory of every mpv in this container, from two readings of /proc a moment apart."""
    now, found = time.time(), {}
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not pid.isdigit():
            continue
        comm = (_read(f"/proc/{pid}/comm") or "").strip()
        stat = (_read(f"/proc/{pid}/stat") or "").rpartition(")")[2].split()
        if comm != "mpv" or len(stat) < 13:
            continue
        cmd = (_read(f"/proc/{pid}/cmdline") or "").replace("\0", " ")
        win = next((w.split("=", 1)[1] for w in cmd.split() if w.startswith("--wayland-app-id=")), "")
        found[pid] = {"pid": int(pid), "window": win, "ticks": int(stat[11]) + int(stat[12]),
                      "rss_mb": int((_read(f"/proc/{pid}/statm") or "0 0").split()[1]) * os.sysconf("SC_PAGE_SIZE") // 1048576}
    out = []
    for pid, p in found.items():
        before = _last.get(("p", pid))
        pct = round((p["ticks"] - before[1]) / os.sysconf("SC_CLK_TCK") / (now - before[0]) * 100) if before and now > before[0] else None
        _last[("p", pid)] = (now, p["ticks"])
        out.append({"pid": p["pid"], "window": p["window"], "cpu_pct": pct, "rss_mb": p["rss_mb"]})
    for k in [k for k in _last if k[0] == "p" and k[1] not in found]:
        del _last[k]  # a player that has gone
    return out


def snapshot():
    t = temperature()
    times, mem = _cpu_times(), meminfo()
    prev, _last["cpu"] = _last.get("cpu"), times
    cpu = round(100 * (times[0] - prev[0]) / (times[1] - prev[1])) if times and prev and times[1] > prev[1] else None
    up = _read("/proc/uptime")
    return {"temp_c": t, "level": level(t), "throttled": throttled(), "cpu_mhz": cpu_mhz(), "cpu_pct": cpu,
            "load": (_read("/proc/loadavg") or "").split()[:3] or None,
            "mem_total_mb": mem.get("MemTotal"), "mem_available_mb": mem.get("MemAvailable"),
            "swap_used_mb": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0) if "SwapTotal" in mem else None,
            "uptime_s": int(float(up.split()[0])) if up else None, "players": players()}
