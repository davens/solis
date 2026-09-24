"""Constants for the Solis Solarman integration."""

DOMAIN = "solis_solarman"

CONF_SERIAL = "serial"
CONF_HOLD_SECONDS = "hold_seconds"

DEFAULT_PORT = 8899
DEFAULT_SCAN_INTERVAL = 10

# How long the entities keep serving the last good sweep after a failed poll
# before going unavailable. Long enough to ride out logger session contention
# and one reconnect (15 s socket timeout), short enough that a held
# instantaneous power - a fabricated measurement - cannot span more than about
# half an hourly statistics bucket. Not exposed by the config flow; set
# hold_seconds in the entry data to override.
DEFAULT_HOLD_SECONDS = 120
