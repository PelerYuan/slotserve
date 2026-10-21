"""What a machine has (CPU cores, memory, GPUs) and how busy it is, without extra dependencies."""

from __future__ import annotations

import contextlib
import ctypes
import ipaddress
import os
import platform
import re
import socket
import struct
import sys
import time
from pathlib import Path

from .. import __version__


def memory_mb() -> tuple[int | None, int | None]:
    """(used, total) RAM in MiB, or (None, None) when unknown."""
    try:
        if sys.platform.startswith("linux"):
            info: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, value = line.partition(":")
                info[key] = int(value.split()[0])  # kB
            total = info.get("MemTotal", 0)
            available = info.get("MemAvailable", info.get("MemFree", 0))
            return (total - available) // 1024, total // 1024
        if os.name == "nt":

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                total = status.ullTotalPhys // (1024 * 1024)
                return total - status.ullAvailPhys // (1024 * 1024), total
    except (OSError, ValueError, AttributeError):
        pass
    return None, None


class CpuMeter:
    """CPU utilization (0-100) between two calls, from /proc/stat or GetSystemTimes."""

    def __init__(self):
        self._last: tuple[float, float] | None = self._times()

    @staticmethod
    def _times() -> tuple[float, float] | None:
        """(idle, total) CPU time since boot."""
        try:
            if sys.platform.startswith("linux"):
                fields = [float(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
                idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
                return idle, sum(fields)
            if os.name == "nt":
                idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
                if ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                    ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
                ):
                    return float(idle.value), float(kernel.value + user.value)  # kernel time includes idle
        except (OSError, ValueError, IndexError, AttributeError):
            pass
        return None

    def percent(self) -> float | None:
        now = self._times()
        last, self._last = self._last, now
        if now is None or last is None or now[1] <= last[1]:
            return None
        busy = 1.0 - (now[0] - last[0]) / (now[1] - last[1])
        return round(max(0.0, min(1.0, busy)) * 100, 1)


# ---- what kind of machine ----------------------------------------------------------------------------------

HOST_OS = ("linux", "windows", "macos")


def _kernel_release() -> str:
    with contextlib.suppress(OSError):
        return Path("/proc/sys/kernel/osrelease").read_text().strip()
    return platform.release()


def detect_platform(host_os_hint: str = "", kernel: str | None = None, os_name: str | None = None) -> dict:
    """Where the agent runs: the host's OS and how (natively, in Docker, in WSL2 / Docker Desktop).

    Docker Desktop on Windows runs containers in a WSL2 VM whose kernel release says "microsoft-standard-WSL2";
    Docker Desktop on a Mac uses a "linuxkit" kernel. The install scripts also pass the host OS explicitly.
    """
    hint = host_os_hint.strip().lower()
    release = (kernel if kernel is not None else _kernel_release()).lower()
    os_name = os_name or sys.platform
    in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    if os_name == "win32":
        host, runtime = "windows", "native"
    elif os_name == "darwin":
        host, runtime = "macos", "native"
    elif "microsoft" in release or "wsl" in release:
        host, runtime = "windows", "wsl2"
    elif "linuxkit" in release:
        host, runtime = (hint if hint in HOST_OS and hint != "linux" else "macos"), "docker-desktop"
    else:
        host, runtime = "linux", "docker" if in_container else "native"
    if hint in HOST_OS and runtime != "native":
        host = hint
    labels = {"linux": "Linux", "windows": "Windows", "macos": "macOS"}
    label = labels[host]
    if runtime == "wsl2":
        label += " · WSL2" + (" (Docker Desktop)" if in_container else "")
    elif runtime == "docker-desktop":
        label += " · Docker Desktop"
    elif runtime == "docker":
        label += " · Docker"
    return {"host_os": host, "runtime": runtime, "in_container": in_container, "label": label}


# ---- network cards (Wake-on-LAN) ---------------------------------------------------------------------------

MAC_RE = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$", re.IGNORECASE)
# Virtual cards: their MAC addresses cannot wake the machine.
_VIRTUAL_NAMES = ("lo", "docker", "br-", "veth", "virbr", "vmnet", "vboxnet", "tailscale", "wg", "tun", "tap", "zt")
_VIRTUAL_OUIS = ("00:15:5d", "02:42")  # Hyper-V / WSL2 virtual cards, Docker bridges


def normalize_mac(value: str) -> str | None:
    value = value.strip()
    if not MAC_RE.match(value):
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def parse_macs(text: str | None) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[\s,;]+", text or ""):
        mac = normalize_mac(part) if part else None
        if mac and mac not in out:
            out.append(mac)
    return out


def physical_mac(mac: str | None) -> bool:
    if not mac or mac == "00:00:00:00:00:00":
        return False
    if mac.startswith(_VIRTUAL_OUIS):
        return False
    return not int(mac[:2], 16) & 0x02  # locally administered = made up by software


def _ipv4(name: str) -> tuple[str, int] | None:
    """(address, prefix length) of a Linux network card."""
    try:
        import fcntl
    except ImportError:
        return None
    packed = struct.pack("256s", name[:15].encode())
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])  # SIOCGIFADDR
            mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, packed)[20:24])  # SIOCGIFNETMASK
        except OSError:
            return None
    return addr, ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen


def network_cards() -> list[dict]:
    """Physical-looking network cards with their MAC and IPv4 network (Linux; empty elsewhere)."""
    root = Path("/sys/class/net")
    if not root.is_dir():
        return []
    cards = []
    for path in sorted(root.iterdir()):
        name = path.name
        if name.startswith(_VIRTUAL_NAMES):
            continue
        try:
            mac = normalize_mac((path / "address").read_text())
        except OSError:
            continue
        ip = _ipv4(name)
        card = {"name": name, "mac": mac, "physical": physical_mac(mac)}
        if ip is not None:
            net = ipaddress.IPv4Network(f"{ip[0]}/{ip[1]}", strict=False)
            card.update(ipv4=ip[0], prefix=ip[1], subnet=str(net), broadcast=str(net.broadcast_address))
        cards.append(card)
    return cards


def magic_packet(mac: str) -> bytes:
    normalized = normalize_mac(mac)
    if normalized is None:
        raise ValueError(f"not a MAC address: {mac!r}")
    return bytes([0xFF]) * 6 + bytes.fromhex(normalized.replace(":", "")) * 16


def send_magic(macs: list[str], broadcasts: list[str], port: int = 9) -> int:
    """Sends a Wake-on-LAN magic packet for each MAC to each broadcast address; returns packets sent."""
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in macs:
            packet = magic_packet(mac)
            for address in broadcasts:
                with contextlib.suppress(OSError):
                    s.sendto(packet, (address, port))
                    sent += 1
    return sent


def machine_info(wol_mac: str = "", network: str = "", host_os: str = "") -> dict:
    """Static facts about this machine (sent once per connection)."""
    _used, total = memory_mb()
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}".strip(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpu_cores": os.cpu_count() or 0,
        "memory_total_mb": total,
        "version": __version__,
        "started_at": time.time(),
        "platform": detect_platform(host_os),
        "networks": network_cards(),
        "wol_macs": parse_macs(wol_mac),
        "network": network.strip()[:64],
    }
