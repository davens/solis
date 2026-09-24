# Network topology, the dual-homed HA Green, SSH and remote access

*Split out of `CLAUDE.md` on 2026-09-13 to keep the always-loaded file small. This is the same text, except that site-specific values are placeholders whose real values are in the gitignored `CLAUDE.local.md`. The rules and traps that apply even when you are NOT reading this file stayed in `CLAUDE.md`.*

*Addresses and identifiers below are placeholders such as `<logger-ip>`; the real values are in the gitignored `CLAUDE.local.md`.*

## Network topology

The retained topology record is partly historical because a bridge migration was attempted but did not
stick:

- FRITZ!Box serves `<fritz-lan>`. Two Tenda Nova meshes split main/fast and 2.4 GHz IoT.
- IoT mesh NATs `<iot-subnet>`, gateway `<iot-gateway>`, WAN `<iot-mesh-wan>`. The logger is `<logger-ip>`. IoT
  can initiate toward the Fritz LAN; main cannot initiate toward the IoT subnet.
- Main-mesh access used the Tenda-app TCP forward **`<iot-mesh-wan>:8899` -> `<logger-ip>:8899`**, verified
  carrying Modbus. **The owner deleted that forward on 2026-09-12**, because the HA box now reaches
  `<iot-subnet>` directly. Nothing else used it.
- **Bridge mode was tried twice with a valid wired uplink and silently reverted to Dynamic. Do not burn
  time repeating it.** If it ever succeeds, the logger will get a Fritz address and `solis_net.py` should
  rediscover it.
- Mesh B's WAN is the Fritz LAN; the FRITZ!Box 7530 AX has zero port mappings and a dynamic public IP, so
  two NATs separate the inverter from the internet.
- **The forward was LAN-only** while it existed -- it never exposed anything to the internet.
- **There is no DHCP reservation anywhere behind the Tenda** -- it rejected the MAC binding, for both the
  logger and the HA Wi-Fi leg. If the logger leaves `<logger-ip>`, anything pinned to that address breaks
  silently; back when the forward existed the symptom was narrower, control failing only from the main
  mesh while still working from IoT, fixed by repointing the forward or making the logger static.
  Likewise, if the HA Wi-Fi lease moves off **`<ha-wifi-ip>`**, anything pinned to that address breaks
  silently.
- Tenda UPnP `AddPortMapping` returns SOAP 500, likely because the target is not the requester. Reading
  mappings works, but app-created forwards do not appear there. Use the app.

The native HA integration stores the configured host directly, has **no reconfigure step**, and has
**no `solis_net.py` fallback** -- unlike `control.py` and `settings_dash.py`, it cannot rediscover the
logger. So a DHCP change requires editing or recreating its config entry. Options are to delete and re-add (loses
entity history) or to edit `/config/.storage/core.config_entries` over SSH with core stopped and restart
(keeps `entry_id`, `unique_id`, entities and history). **Prefer the second.** The entry was repointed off
the deleted forward and now reads host **`<logger-ip>`**, state `loaded` (verified 2026-09-13). The Docker
compose host was deliberately the forwarded `<iot-mesh-wan>` in the recorded configuration; compose is now
meant to take `SOLIS_HOST` from `.env`.

**Keep the reason that repointing was urgent, because the failure mode generalises.** After the forward
was deleted the entry still pointed at `<iot-mesh-wan>` and kept working anyway -- the integration holds
ONE long-lived Solarman TCP session, and an established NAT conntrack entry outlived the deleted
forward. It would have failed at the next *reconnect*, not the next read: any read error calls
`_drop_session()`, and the reconnect would have gone to a host that no longer forwards. A long-lived
connection surviving a deleted route is not evidence the route still exists.

### The HA Green is dual-homed (2026-09-12)

A USB Wi-Fi adapter puts the HA Green on the IoT mesh **as well as** the main mesh, which is what makes
`<iot-subnet>` reachable from HA without the Tenda forward. Ethernet stays the primary path; the Wi-Fi
leg is for reaching IoT devices only.

- Adapter is a TP-Link micro, USB ID **0bda:8179** = Realtek **RTL8188EU rev D**, 2.4 GHz 802.11n 1T1R. It
  needs **no driver work**: the in-tree `rtl8xxxu` binds it and HAOS already ships
  `rtlwifi/rtl8188eufw.bin`. 2.4 GHz-only is not a limitation -- the IoT mesh is 2.4 GHz.
- **NetworkManager renames it `wlan0` -> `wlu1`.** Every command and any future config must say `wlu1`.
- **Supervisor does not notice a newly plugged adapter until `ha network reload`.** Before that,
  `ha network info` lists only `end0` and the Network settings page shows no Wi-Fi tab, which reads
  exactly like an unsupported adapter. Check the kernel log before concluding that:
  `ha host logs --boot 0 | grep -iE "8188|8xxxu|wlu1"`.
- Live config: `wlu1` DHCP on SSID **`<iot-ssid>`**, WPA-PSK, lease **`<ha-wifi-ip>/24`**, gw `<iot-gateway>`.
  `end0` keeps `<ha-eth-ip>/24` and, importantly, keeps `primary: true`.
- **Ethernet must stay the default route and does so on its own.** NetworkManager's default metrics are
  100 for Ethernet against 600 for Wi-Fi, so the Tenda's gateway loses. Verified after connecting:
  `primary` stayed on `end0`, and 1.1.1.1 still routes out via the Fritz rather than through the IoT
  mesh's second NAT. **Do not set a static IP with a gateway on `wlu1` to "fix" this**; there is nothing
  to fix, and a second default route is how it would break.
- Verified reachable from HA after connecting: `<iot-gateway>`, **`<logger-ip>` (the logger)**, `<fritz-gateway>`,
  1.1.1.1 -- all OK.
- **systemd-resolved picks up the Tenda as a second DNS server** (`<iot-gateway>`, search domain
  `tendawifi.com`) and marks it default-route. Resolution works, so this is left alone, but it is the
  first suspect for any odd lookup behaviour.

### SSH access to /config

Opened 2026-09-07 to install `gas_hybrid`. The `core_ssh` add-on was already installed and running but
ingress-only, with no authorized key and `22/tcp` unmapped. It now carries the dev machine's
`~/.ssh/id_ed25519.pub`, maps **22/tcp -> 22222**, keeps `password` empty (key-only) and `tcp_forwarding`
off: `ssh -p 22222 root@homeassistant.local`. Set through the **websocket** Supervisor API
(`supervisor/api` -> `/addons/core_ssh/options`, then `/restart`), which works where the REST proxy still
401s.

`ha core restart` from that shell takes about **four minutes** to come back, and `ha core logs` is the way
to read the log -- HAOS keeps no current `/config/home-assistant.log`, only rotated `.1`/`.old` files.

**Add-ons are called "Apps" in this HA build and live under `/config/apps`, not `/hassio/`.** An add-on's
page is `/config/app/<slug>/info` and its ingress UI is `/app/<slug>`; `/hassio/addon/<slug>/info` returns
a bare `404`, and `get_panels` confirms there is no `hassio` panel registered. The Supervisor REST proxy
at `/api/hassio/...` returns 401 to a long-lived token, but the **websocket** command
`{"type": "supervisor/api", "endpoint": "/addons", "method": "get"}` works, and is how the add-on
install, start and info reads recorded here were done. It only handles JSON
responses, so `/addons/<slug>/logs` fails with a bare `unknown_error`; read an add-on's own API through
ingress instead, using a session from `POST /ingress/session`.

### Remote access: Tailscale, HA box only

Installed 2026-08-29: the Community Add-ons Tailscale app, `a0d7b954_tailscale` v0.29.0, boot `auto`. The
node is `homeassistant` on tailnet `<tailnet>.ts.net` at **`<tailnet-ip>`**, authenticated as the owner's
account (named in `CLAUDE.local.md`). Remote URL is `http://homeassistant.<tailnet>.ts.net:8123`.
**Key expiry is disabled** for this device and must stay disabled, because the failure mode is the node silently dropping off the
tailnet months later with no error anywhere.

**`advertise_routes` is empty on purpose. Do not add subnet routes.** The owner's instruction on
2026-08-29 was "i only need the ha box, for security". Advertising `<fritz-lan>` would put the
inverter's then-forwarded Modbus port **`<iot-mesh-wan>:8899`** within reach of every tailnet device;
that is exactly what is being declined. This
does not need re-proposing. None of it creates a write path to the inverter: Tailscale reaches HA, and HA
is read-only.

### Fire Stick

The Fire TV, **`<firetv-ip>`**, MAC `<firetv-mac>`, Fritz hostname
`<firetv-hostname>`, no DHCP reservation. ADB is enabled: `adb connect <firetv-ip>:5555`.
Find it with mDNS (`dns-sd -B _amzn-wplay._tcp.`) rather than by hostname -- the Fritz name does not
identify it, and the ARP table alone is ambiguous. Nothing here depends on it; it is recorded because
it is on the same LAN.

