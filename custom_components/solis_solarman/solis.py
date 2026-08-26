"""Read-only Modbus sweep of a Solis hybrid inverter via a Solarman logger.

Register map verified live 2026-08-21 by closing the energy balance - see
CLAUDE.md at the repository root. Everything here is a read; this integration
has no write path at all.
"""
from pysolarmanv5 import PySolarmanV5

# Input registers (FC 0x04)
TELEMETRY_BASE = 33133
TELEMETRY_COUNT = 18          # 33133..33150
REG_BATTERY_VOLTAGE = 33133   # x0.1 V
REG_BATTERY_CURRENT = 33134   # x0.1 A - NOT 33135, which is the direction flag
REG_BATTERY_DIRECTION = 33135  # 0 = charging, 1 = discharging
REG_BATTERY_SOC = 33139
REG_BATTERY_SOH = 33140
REG_HOUSE_LOAD = 33147
REG_BATTERY_POWER = 33149     # u32 across 33149/33150, watts, unsigned

PV_BASE = 33049
PV_COUNT = 10                 # four string V/A pairs, then total power
REG_PV_POWER = 33057          # u32 across 33057/33058, watts (DC side)

REG_GRID_VOLTAGE = 33073      # x0.1 V
REG_ENERGY_TODAY = 33035      # x0.1 kWh; 33036 is yesterday

ENERGY_DAY_BASE = 33161
ENERGY_DAY_COUNT = 20         # 33161..33180
REG_BATT_CHARGE_TODAY = 33163    # x0.1 kWh; 33161/33162 are the u32 total
REG_BATT_DISCHARGE_TODAY = 33167  # x0.1 kWh
REG_GRID_IMPORT_TODAY = 33171  # x0.1 kWh
REG_GRID_EXPORT_TODAY = 33175  # x0.1 kWh
REG_HOUSE_TODAY = 33179        # x0.1 kWh
REG_HOUSE_YESTERDAY = 33180    # x0.1 kWh

# Smart meter grid power, s32 across 33257/33258: positive exporting. Never
# derive grid flow from PV and battery - those are DC registers and the
# inverter's conversion loss reads as phantom export.
REG_METER_POWER = 33257

# Holding registers (FC 0x03)
REG_MODE = 43110              # work-mode bitfield; bit1 = time-of-use charging
REG_CHARGE_CURRENT = 43141    # x0.1 A
SLOT1_CHARGE = 43143          # h,m start / h,m end

MODE_BITS = {
    0: "self-use",
    1: "time-of-use charging",
    2: "off-grid",
    3: "battery wake-up",
    4: "backup/reserve",
    5: "grid charging allowed",
    6: "feed-in priority",
    9: "battery healing",
}


class SolisClient:
    """One Solarman V5 session; the logger allows exactly one at a time."""

    def __init__(self, host, serial, port=8899):
        self.host = host
        self.serial = int(serial)
        self.port = int(port)
        self._modbus = None

    def _session(self):
        if self._modbus is None:
            self._modbus = PySolarmanV5(
                self.host, self.serial, port=self.port,
                mb_slave_id=1, socket_timeout=15, verbose=False)
        return self._modbus

    def close(self):
        if self._modbus is not None:
            try:
                self._modbus.disconnect()
            except Exception:
                pass
            self._modbus = None

    def read_all(self):
        """One full sweep. Blocking - run in an executor. Raises on failure."""
        try:
            return self._read()
        except Exception:
            # Null-and-retry: forget the session so the next poll reconnects.
            self.close()
            raise

    def _read(self):
        modbus = self._session()
        block = modbus.read_input_registers(
            register_addr=TELEMETRY_BASE, quantity=TELEMETRY_COUNT)
        pv = modbus.read_input_registers(register_addr=PV_BASE, quantity=PV_COUNT)
        grid_voltage = modbus.read_input_registers(
            register_addr=REG_GRID_VOLTAGE, quantity=1)[0]
        energy = modbus.read_input_registers(register_addr=REG_ENERGY_TODAY, quantity=2)
        day = modbus.read_input_registers(
            register_addr=ENERGY_DAY_BASE, quantity=ENERGY_DAY_COUNT)
        meter = modbus.read_input_registers(register_addr=REG_METER_POWER, quantity=2)
        mode = modbus.read_holding_registers(register_addr=REG_MODE, quantity=1)[0]
        charge = modbus.read_holding_registers(
            register_addr=REG_CHARGE_CURRENT, quantity=1)[0]
        slot1 = modbus.read_holding_registers(register_addr=SLOT1_CHARGE, quantity=4)

        def tele(addr):
            return block[addr - TELEMETRY_BASE]

        def u32(base, words, first):
            i = base - first
            return (words[i] << 16) + words[i + 1]

        charging = tele(REG_BATTERY_DIRECTION) == 0
        battery_power = u32(REG_BATTERY_POWER, block, TELEMETRY_BASE)

        meter_power = (meter[0] << 16) + meter[1]
        if meter_power >= 1 << 31:
            meter_power -= 1 << 32  # s32; unsigned reads show imports as ~4.29e9

        pv1 = round(pv[0] * 0.1 * pv[1] * 0.1)
        pv2 = round(pv[2] * 0.1 * pv[3] * 0.1)

        slot1_unset = slot1 == [0, 0, 0, 0]
        return {
            "battery_voltage": round(tele(REG_BATTERY_VOLTAGE) * 0.1, 1),
            "battery_current": round(tele(REG_BATTERY_CURRENT) * 0.1, 1),
            # Signed: positive charging, negative discharging.
            "battery_power": battery_power if charging else -battery_power,
            "battery_charging": charging and battery_power >= 10,
            "battery_soc": tele(REG_BATTERY_SOC),
            "battery_soh": tele(REG_BATTERY_SOH),
            "house_load": tele(REG_HOUSE_LOAD),
            "pv_power": u32(REG_PV_POWER, pv, PV_BASE),
            "pv1_power": pv1,
            "pv2_power": pv2,
            "grid_voltage": round(grid_voltage * 0.1, 1),
            # Signed: positive importing, negative exporting (house convention).
            "grid_power": -meter_power,
            "solar_today": round(energy[0] * 0.1, 1),
            "solar_yesterday": round(energy[1] * 0.1, 1),
            "battery_charge_today": round(day[REG_BATT_CHARGE_TODAY - ENERGY_DAY_BASE] * 0.1, 1),
            "battery_discharge_today": round(day[REG_BATT_DISCHARGE_TODAY - ENERGY_DAY_BASE] * 0.1, 1),
            "grid_import_today": round(day[REG_GRID_IMPORT_TODAY - ENERGY_DAY_BASE] * 0.1, 1),
            "grid_export_today": round(day[REG_GRID_EXPORT_TODAY - ENERGY_DAY_BASE] * 0.1, 1),
            "house_today": round(day[REG_HOUSE_TODAY - ENERGY_DAY_BASE] * 0.1, 1),
            "house_yesterday": round(day[REG_HOUSE_YESTERDAY - ENERGY_DAY_BASE] * 0.1, 1),
            "mode": mode,
            "mode_bits": [MODE_BITS.get(b, f"bit{b}") for b in range(16) if mode >> b & 1],
            "timed_charging": bool(mode >> 1 & 1),
            "charge_current": round(charge * 0.1, 1),
            "charge_window": None if slot1_unset
            else f"{slot1[0]:02d}:{slot1[1]:02d}-{slot1[2]:02d}:{slot1[3]:02d}",
        }
