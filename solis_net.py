"""Locate and connect to the Solis logger without hard-coding its address.

The logger's IP is DHCP and has moved more than once (192.0.2.48 -> .45, and
again after the mesh was bridged), so resolve it by serial number instead.
Order: $SOLIS_HOST, then a Solarman UDP broadcast, then LAST_KNOWN.
"""
import os
import socket

from pysolarmanv5 import PySolarmanV5

SERIAL_NUMBER = 0000000000
LOGGER_MAC = "000000000000"
PORT = 8899
SLAVE_ID = 1
# Tried in order when discovery finds nothing. The logger sits on the 2.4 GHz
# IoT mesh (192.0.2.x); from the main mesh it is reached through a port
# forward on that mesh's WAN address. See CLAUDE.md.
CANDIDATES = ["192.0.2.45", "198.51.100.49"]

DISCOVERY_REQUEST = b"WIFIKIT-214028-READ"
DISCOVERY_PORT = 48899


def discover(serial=SERIAL_NUMBER, timeout=3.0):
    """Broadcast for Solarman loggers; return the IP matching serial, else None.

    Only works on the same layer-2 segment as the logger.
    """
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
            if found_serial == str(serial) or mac.upper() == LOGGER_MAC:
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
    override = os.environ.get("SOLIS_HOST")
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
    raise SystemExit(
        "cannot reach the logger. Tried discovery and "
        f"{', '.join(CANDIDATES)}. Join the IoT mesh, or set SOLIS_HOST."
    )


def connect(host=None):
    if host is None:
        host = resolve_host()
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
