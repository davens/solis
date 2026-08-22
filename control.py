"""Direct Modbus control of the Solis inverter (replaces the MQTT/Node-RED path).

Writes are refused unless --apply is passed; without it every command is a dry run
that shows the before/after values it would set.

  uv run --no-project --with pysolarmanv5 python control.py show
  uv run --no-project --with pysolarmanv5 python control.py charge-current 50 --apply
  uv run --no-project --with pysolarmanv5 python control.py window 1 charge 23:30 04:30 --apply
"""
import argparse
import datetime
import sys

import solis_net

REG_MODE = 43110
REG_CHARGE_CURRENT = 43141
REG_DISCHARGE_CURRENT = 43142
REG_SLOT_BASE = 43143  # slot n: base + (n-1)*8; +0 chg start, +2 chg end, +4 dis start, +6 dis end
SLOT_STRIDE = 8
CURRENT_SCALING = 0.1

# Real-time clock: year, month, day, hour, minute, second, one per register,
# year held as two digits. Verified live 2026-08-21 - the block decoded as a
# valid date on the first read, which is why it can be trusted without a
# manufacturer document.
REG_CLOCK = 43000
CLOCK_REGS = 6

# Below this the clock is not worth a write. The charge window is enforced by
# the inverter's own clock, so drift shifts the whole off-peak window against
# the tariff - but a few seconds either way costs nothing and rewriting the
# registers nightly is churn for its own sake.
CLOCK_TOLERANCE_SECONDS = 60

# Bit meanings for the work-mode register (43110).
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

# Registers we refuse to touch: DNO-mandated grid protection (G98/G99).
PROTECTED = set(range(43038, 43050)) | set(range(43090, 43098))

MAX_CURRENT_AMPS = 100.0


def write_reg(modbus, addr, value, apply):
    """Write one holding register, or describe the write if apply is False."""
    if addr in PROTECTED:
        raise SystemExit(f"refusing to write {addr}: grid protection register")
    current = modbus.read_holding_registers(register_addr=addr, quantity=1)[0]
    if current == value:
        print(f"  {addr}: already {value}, no change")
        return
    if apply:
        modbus.write_holding_register(register_addr=addr, value=value)
        readback = modbus.read_holding_registers(register_addr=addr, quantity=1)[0]
        status = "ok" if readback == value else f"MISMATCH (read back {readback})"
        print(f"  {addr}: {current} -> {value}  [{status}]")
    else:
        print(f"  {addr}: {current} -> {value}  [dry run]")


def parse_hhmm(text):
    try:
        hours, minutes = text.split(":")
        hours, minutes = int(hours), int(minutes)
    except ValueError:
        raise SystemExit(f"bad time {text!r}, expected HH:MM")
    if not 0 <= hours <= 23:
        raise SystemExit(f"hour out of range in {text!r}")
    if not 0 <= minutes <= 59:
        raise SystemExit(f"minute out of range in {text!r}")
    return hours, minutes


def slot_addr(slot, kind):
    if not 1 <= slot <= 3:
        raise SystemExit(f"slot must be 1-3, got {slot}")
    base = REG_SLOT_BASE + (slot - 1) * SLOT_STRIDE
    if kind == "charge":
        return base
    return base + 4


def read_clock(modbus):
    """Return the inverter's own idea of the time, or None if it does not decode."""
    regs = modbus.read_holding_registers(register_addr=REG_CLOCK, quantity=CLOCK_REGS)
    year, month, day, hour, minute, second = regs
    try:
        return datetime.datetime(2000 + year, month, day, hour, minute, second)
    except ValueError:
        return None


def cmd_set_time(modbus, args):
    """Set the inverter clock from this machine's local time.

    Six separate register writes, so the seconds field is already a second or
    two stale by the time the last one lands. That is well inside the tolerance
    that matters here - the point is the minutes, because the off-peak charge
    window is timed by this clock and drift moves the whole window against the
    tariff.
    """
    now = datetime.datetime.now()
    inverter = read_clock(modbus)
    if inverter is None:
        print(f"inverter clock at {REG_CLOCK} does not decode as a date")
    else:
        drift = (inverter - now).total_seconds()
        print(f"inverter clock {inverter:%Y-%m-%d %H:%M:%S}")
        print(f"local clock    {now:%Y-%m-%d %H:%M:%S}")
        print(f"drift          {drift:+.0f} s")
        if abs(drift) < CLOCK_TOLERANCE_SECONDS and not args.force:
            print(f"within {CLOCK_TOLERANCE_SECONDS} s - nothing to do (--force to write anyway)")
            return

    print(f"set inverter clock to {now:%Y-%m-%d %H:%M:%S}")
    fields = (now.year - 2000, now.month, now.day, now.hour, now.minute, now.second)
    for offset, value in enumerate(fields):
        write_reg(modbus, REG_CLOCK + offset, value, args.apply)


def cmd_show(modbus, _args):
    inverter = read_clock(modbus)
    now = datetime.datetime.now()
    if inverter is None:
        print(f"clock {REG_CLOCK}: does not decode as a date")
    else:
        drift = (inverter - now).total_seconds()
        note = "ok" if abs(drift) < CLOCK_TOLERANCE_SECONDS else "SET-TIME"
        print(f"clock {REG_CLOCK} = {inverter:%Y-%m-%d %H:%M:%S}  ({drift:+.0f} s vs local, {note})")

    mode = modbus.read_holding_registers(register_addr=REG_MODE, quantity=1)[0]
    bits = [MODE_BITS.get(b, f"bit{b}") for b in range(16) if mode >> b & 1]
    print(f"work mode {REG_MODE} = {mode}: {', '.join(bits) if bits else 'none'}")

    charge = modbus.read_holding_registers(register_addr=REG_CHARGE_CURRENT, quantity=1)[0]
    discharge = modbus.read_holding_registers(register_addr=REG_DISCHARGE_CURRENT, quantity=1)[0]
    print(f"charge current    {REG_CHARGE_CURRENT} = {charge * CURRENT_SCALING:.1f} A")
    print(f"discharge current {REG_DISCHARGE_CURRENT} = {discharge * CURRENT_SCALING:.1f} A")

    print("time windows:")
    for slot in (1, 2, 3):
        for kind in ("charge", "discharge"):
            addr = slot_addr(slot, kind)
            vals = modbus.read_holding_registers(register_addr=addr, quantity=4)
            start = f"{vals[0]:>2}:{vals[1]:02d}"
            end = f"{vals[2]:>2}:{vals[3]:02d}"
            unset = " (unset)" if vals == [0, 0, 0, 0] else ""
            print(f"  slot {slot} {kind:9} {addr}: {start} -> {end}{unset}")


def cmd_current(modbus, args):
    if not 0 <= args.amps <= MAX_CURRENT_AMPS:
        raise SystemExit(f"current must be 0-{MAX_CURRENT_AMPS} A, got {args.amps}")
    addr = REG_CHARGE_CURRENT if args.command == "charge-current" else REG_DISCHARGE_CURRENT
    print(f"set {args.command} to {args.amps} A")
    write_reg(modbus, addr, round(args.amps / CURRENT_SCALING), args.apply)


def cmd_window(modbus, args):
    start_h, start_m = parse_hhmm(args.start)
    end_h, end_m = parse_hhmm(args.end)
    addr = slot_addr(args.slot, args.kind)
    print(f"set slot {args.slot} {args.kind} window to {args.start} -> {args.end}")
    for offset, value in enumerate((start_h, start_m, end_h, end_m)):
        write_reg(modbus, addr + offset, value, args.apply)


def cmd_clear_window(modbus, args):
    addr = slot_addr(args.slot, args.kind)
    print(f"clear slot {args.slot} {args.kind} window")
    for offset in range(4):
        write_reg(modbus, addr + offset, 0, args.apply)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="print current battery/tariff settings")

    for name in ("charge-current", "discharge-current"):
        p = sub.add_parser(name, help=f"set {name.replace('-', ' ')} in amps")
        p.add_argument("amps", type=float)

    p = sub.add_parser("window", help="set a charge/discharge time window")
    p.add_argument("slot", type=int, choices=(1, 2, 3))
    p.add_argument("kind", choices=("charge", "discharge"))
    p.add_argument("start")
    p.add_argument("end")

    p = sub.add_parser("clear-window", help="zero a charge/discharge time window")
    p.add_argument("slot", type=int, choices=(1, 2, 3))
    p.add_argument("kind", choices=("charge", "discharge"))

    p = sub.add_parser("set-time", help="set the inverter clock from this machine")
    p.add_argument("--force", action="store_true",
                   help=f"write even when drift is under {CLOCK_TOLERANCE_SECONDS} s")

    args = parser.parse_args()
    handlers = {
        "show": cmd_show,
        "charge-current": cmd_current,
        "discharge-current": cmd_current,
        "window": cmd_window,
        "clear-window": cmd_clear_window,
        "set-time": cmd_set_time,
    }

    modbus = solis_net.connect()
    handlers[args.command](modbus, args)

    if not args.apply and args.command != "show":
        print("\ndry run - nothing written. Re-run with --apply to commit.")


if __name__ == "__main__":
    sys.exit(main())
