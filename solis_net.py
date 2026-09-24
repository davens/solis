"""Locate and connect to the Solis logger without hard-coding its address.

The logger's address is assigned dynamically, so resolve it by serial number
or MAC address instead. See CLAUDE.local.md for site-specific network notes.
Order: $SOLIS_HOST, then a Solarman UDP broadcast, then configured candidates.
"""
import socket

from pysolarmanv5 import PySolarmanV5

import site_env


_serial = site_env.get("SOLIS_LOGGER_SERIAL")
try:
    SERIAL_NUMBER = int(_serial) if _serial else None
except ValueError as exc:
    raise RuntimeError("SOLIS_LOGGER_SERIAL must be an integer") from exc
LOGGER_MAC = (site_env.get("SOLIS_LOGGER_MAC") or "").replace(":", "").upper()
PORT = 8899
SLAVE_ID = 1
# Tried in order when discovery finds nothing. See CLAUDE.local.md.
CANDIDATES = [
    host.strip()
    for host in (site_env.get("SOLIS_HOST_CANDIDATES") or "").split(",")
    if host.strip()
]

DISCOVERY_REQUEST = b"WIFIKIT-214028-READ"
DISCOVERY_PORT = 48899


def discover(serial=SERIAL_NUMBER, timeout=3.0):
    """Broadcast for Solarman loggers; return a matching IP, else None.

    Only works on the same layer-2 segment as the logger.
    """
    if serial is None and not LOGGER_MAC:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(DISCOVERY_REQUEST, ("<broadcast>", DISCOVERY_PORT))
        while True:
            try:
                data = sock.recv(1024)
            except socket.timeout:
                return None
            parts = data.decode(errors="replace").strip().split(",")
            if len(parts) < 3:
                continue
            ip, mac, found_serial = parts[0], parts[1], parts[2]
            normalised_mac = mac.replace(":", "").upper()
            if ((serial is not None and found_serial == str(serial))
                    or (LOGGER_MAC and normalised_mac == LOGGER_MAC)):
                return ip
    finally:
        sock.close()


def _port_open(host, port=PORT, timeout=2.0):
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def resolve_host(verbose=True):
    """Return the logger's address: override, then discovery, then candidates."""
    override = site_env.get("SOLIS_HOST")
    if override:
        return override
    found = discover()
    if found:
        return found
    for host in CANDIDATES:
        if _port_open(host):
            if verbose:
                print(f"discovery found nothing; reaching logger via {host}")
            return host
    if SERIAL_NUMBER is None and not LOGGER_MAC:
        discovery_note = (
            "Discovery was skipped because neither SOLIS_LOGGER_SERIAL nor "
            "SOLIS_LOGGER_MAC is configured. "
        )
    else:
        discovery_note = "Discovery found no matching logger. "
    candidate_note = (
        f"Tried candidates: {', '.join(CANDIDATES)}. " if CANDIDATES else ""
    )
    raise SystemExit(
        discovery_note + candidate_note
        + "Set SOLIS_HOST or configure fallbacks in SOLIS_HOST_CANDIDATES."
    )


def connect(host=None):
    if host is None:
        host = resolve_host()
    if SERIAL_NUMBER is None:
        raise RuntimeError(
            "SOLIS_LOGGER_SERIAL is required to connect; set it in the "
            "environment or copy its example from .env.example into .env"
        )
    return PySolarmanV5(
        address=host,
        serial=SERIAL_NUMBER,
        port=PORT,
        mb_slave_id=SLAVE_ID,
        verbose=False,
        # The library default is 60 s per read. A dashboard sweep is ~11 reads,
        # so a dead network held the poller (and its write lock) for many
        # minutes before the error surfaced. LAN reads are sub-second; 10 s is
        # generous and turns an outage into a fast fail-and-reconnect.
        socket_timeout=10,
    )


if __name__ == "__main__":
    print(resolve_host())
