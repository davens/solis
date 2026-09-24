# The Modbus register map, and what the daily counters do and do not prove

*Split out of `CLAUDE.md` on 2026-09-13 to keep the always-loaded file small. This is the same text, unchanged. The rules and traps that apply even when you are NOT reading this file stayed in `CLAUDE.md`.*

## Register map (verified live 2026-08-21)

Input registers use FC 0x04 for telemetry; 43xxx holding registers use FC 0x03/0x06 for settings.

| Register | Meaning |
|---|---|
| 33035 / 33036 | Solar generated today / yesterday, x0.1 kWh; today resets at midnight |
| 33049 / 33050 | PV string 1 voltage/current, x0.1 |
| 33051 / 33052 | PV string 2 voltage/current, x0.1; strings 3-4 at 33053-33056 are unused |
| **33057 + 33058** | Total PV power, W, u32 pair, **DC side** |
| 33071 | DC bus voltage, x0.1; 397.7 V magnitude inference, not confirmed |
| 33073 | Grid phase A voltage, x0.1; mirrored at 33137 |
| **33079 + 33080** | Inverter AC output power, W, u32 pair; AC counterpart of PV power |
| 33094 | Grid frequency, x0.01; 50.10 Hz magnitude inference, not confirmed |
| 33133 / 33134 | Battery voltage/current, x0.1 |
| **33135** | Battery direction: 0 charging, 1 discharging; **not current** |
| 33139 / 33140 | Battery SOC/SOH, % |
| 33147 | House load, W |
| **33149 + 33150** | Battery power, W, big-endian u32 unsigned magnitude; sign from 33135 |
| 33161-33164 | Battery charge energy: u32 total, today, yesterday; x0.1 kWh |
| 33165-33168 | Battery discharge energy, same layout |
| 33169-33172 | Grid import energy, same layout |
| 33173-33176 | Grid export energy, same layout |
| 33177-33180 | House consumption energy, same layout |
| 33251 / 33252 | Meter voltage x0.1 / current x0.01 |
| **33257 + 33258** | Grid power, W, s32: positive exporting, negative importing; mirrored at 33263/33264 |
| 33283-33286 | Meter import/export totals, x0.001 kWh; same energy as 33169-33176 at finer resolution |
| 43010 / 43011 | Battery capacity, Ah / type code |
| 43012 / 43013 | Battery profile capability: 100.0 A each way, 1C |
| 43110 | Work-mode bits. **Verified:** bit0 self-use, bit1 time-of-use charging, bit5 grid charging allowed, bit9 battery healing. `solis.py`'s `MODE_BITS` also names bit2 off-grid, bit3 battery wake-up, bit4 backup/reserve, bit6 feed-in priority -- those four are **unverified** on this inverter. Unknown set bits surface as `bitN`. |
| 43141 / 43142 | Time-of-use charge/discharge current limit, x0.1 A |
| 43143 + (n-1)x8 | Slot n, n=1..3: charge start +0, charge end +2, discharge start +4, discharge end +6; each is hour,minute |

PV and battery power were confirmed by closing the instantaneous balance: 2033 W PV + 1063 W battery =
3008 W house, with string products 358.4 V x 3.5 A and 236.0 V x 3.3 A, battery 52.9 V x 20.1 A.

The slot stride and hour/minute layout were decoded from the live slot-1 window: 23:30-04:30 when first
read, later 23:30-03:30. Slots 2-3 and **every discharge window** were unset deliberately. Read the live
charge end; do not assume those dated observations equal the intended 23:30-05:30 tariff policy.

Grid s32 and sign were confirmed twice. At clear-sky export, 5834 W DC, 5800 W AC and 875 W house
implied 4925 W export; the register read +4984. During import, treating it unsigned produced
4294967253 W = 2^32-43, therefore -43 W -- about 4.29 billion watts if mishandled. An older fallback
took magnitude here and inferred direction from `house_load > ac_power`; it worked but is no longer
needed. The integration negates the meter sign only to match HA's positive-import convention.

**Do not replace the meter with `house_load - pv_power +/- battery_power`.** PV and battery are DC-side;
about 2% conversion loss, more than 100 W near full output, becomes phantom export. This was caught when
the display claimed 108 W export while a 1263 W battery discharge covered a 3840 W house load. If an AC
balance is needed, use 33079/33080.

Daily counters were identified by closing yesterday's balance: 31.7 solar + 15.2 import + 6.2 battery out
= 53.1 versus 21.8 house + 22.7 export + 8.2 battery in = 52.7 kWh, 0.4 kWh apart. Register 33036
independently agreed with the owner's 32 kWh actual for 2026-08-20, pinning x0.1 scaling.

### What the daily counters do and do not prove

The daily "today" values -- 33171 import, 33179 house, 33163 battery charge, 33167 discharge, 33175
export -- are **single u16 words**, not u32 pairs, so a word-order bug is structurally impossible for
them. Their *meaning*, however, rests entirely on the closed energy balance above and not on any vendor
document. 33283-33286 are not polled by anything here and are **suspected** to mirror only the two
lifetime u32 totals, not the daily ones.

**Solis Cloud disagrees with the registers on daily import; trust the registers.** On 2026-08-27 cloud
showed 8 kWh against 14.0 kWh from 33171. The 00:00-01:00 BST hour alone holds 6.1 kWh and 14.0 - 6.1 =
7.9, which rounds to cloud's 8, so the likely cause is cloud's day boundary sitting an hour off -- the
register resets at about 23:59:53 BST. This is **unproven**: shifting every field by the same hour does
not reproduce cloud's consumption (19.0 against 17.8) or battery charge (4.2 against 5). Do not adjust a
register reading to agree with the cloud figure.

**The house-consumption counter 33177-33180 is a derived residual, and it silently contains the
inverter's conversion loss. Never compare it against integrated house-load power, and never treat it as
an independent measurement in an energy balance.** Established 2026-08-31; this supersedes the earlier
note that the gap was "two different measurement paths, left unattributed".

Across five consecutive days the counter equals `solar + import + discharge - export - charge` to within
+0.1 to +0.4 kWh, always signed the same way:

| date | house counter | S+I+D-E-C | diff |
|---|---|---|---|
| 2026-08-26 | 29.1 | 29.50 | +0.40 |
| 2026-08-27 | 13.1 | 13.30 | +0.20 |
| 2026-08-28 | 20.0 | 20.30 | +0.30 |
| 2026-08-29 | 46.0 | 46.10 | +0.10 |
| 2026-08-30 | 1.0 | 1.10 | +0.10 |

Because it is a residual it absorbs everything the other five counters do not account for, which is
overwhelmingly DC/AC conversion loss and inverter housekeeping. That is the whole of the roughly
0.9 kWh/day gap previously recorded here as unexplained (overnight 2026-08-27: 11.23 kWh integrating
33147 against 12.1 kWh on the counter). The earlier finding that it is **not** a Riemann-method or
sampling artifact still stands and is what forced this explanation -- left, right and trapezoid rules
span only 0.012 kWh, and the largest sample gap was 92.8 s at about 250 W. **33147 is the honest house
number; 33179 is house plus loss.**

Two consequences that will otherwise mislead:

- **An energy balance built from the six daily counters closes by construction and proves nothing.** It
  cannot detect a conversion loss, because the loss is already inside the house term. The closed balance
  recorded above identifies what the counters *are*; it is not evidence that they are independent.
- **The counters imply impossible efficiency if read literally.** On the 2026-08-31 grid-charging night,
  5.4 kWh imported against 4.4 kWh DC stored and 1.0 kWh house leaves zero loss; taken the other way,
  storing 4.4 kWh DC at the measured 0.887 needs 4.96 kWh AC, which with a 1.0 kWh house overspends the
  5.4 kWh import by 0.56 kWh.

The power sensors do not share the fault. Integrating 5-minute statistics over 23:00-01:05 UTC that night
gave grid 5.443 kWh against a 5.4 counter and battery 4.348 against 4.4 -- both agree -- while house load
integrated to **0.503 kWh against a 1.0 counter**. Only the house pair disagrees, and the power path
reproduces the independently measured charge efficiency: 4.348 / (5.443 - 0.503) = **88.0%**, against
ETA_CHARGE 0.887.

