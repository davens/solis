"""Polling coordinator with a short last-known-good hold.

The logger allows exactly one Modbus session, so a competing reader makes a
sweep fail with a bare `_queue.Empty`, and the reconnect after that can cost a
15 s socket timeout. Dropping all 23 entities to unavailable for one or two
such polls costs more than it saves: the Sankey flow templates lose their
inputs, and a link whose value entity has no statistics rows returns null,
which becomes NaN and silently zeroes every later ribbon from that source.

So for a bounded window the entities keep serving the last good sweep, and
only then fall through to genuinely unavailable - a real outage must still
surface. Nothing here reads or writes anything new; it only changes when
`available` goes false.
"""
import logging

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class SolisCoordinator(DataUpdateCoordinator):
    """Inverter poller that rides out a brief loss of the logger."""

    def __init__(self, hass, name, update_method, update_interval, hold_seconds):
        super().__init__(
            hass, _LOGGER, name=name,
            update_interval=update_interval,
            update_method=update_method)
        self._hold_seconds = hold_seconds
        self._last_good = None

    async def _async_update_data(self):
        data = await super()._async_update_data()
        # Monotonic clock: neither a DST step nor an NTP correction may move
        # the hold window. See the DST trap in CLAUDE.md.
        self._last_good = self.hass.loop.time()
        return data

    @property
    def holding_last_good(self) -> bool:
        """True while a failed poll is still inside the hold window."""
        if self.last_update_success or self._last_good is None:
            return False
        return self.hass.loop.time() - self._last_good <= self._hold_seconds
