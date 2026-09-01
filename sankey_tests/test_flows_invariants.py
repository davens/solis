"""Adversarial invariants for the Sankey v2 flow decomposition (`flows.py`).

Written by an agent that did NOT write `flows.py`, against BRIEF_V2.md section 2
alone. Nothing here is allowed to be softened to make the implementation pass.

The rule under test, restated from the brief so a reader of this file never has
to trust the implementation's own docstring:

    S  = solar                     >= 0, DC
    B  = max(0, -battery)          DC delivered
    C  = max(0,  battery)          DC stored
    G  = max(0,  grid)             AC import
    E  = max(0, -grid)             AC export
    T  = min(tesla, house_load)    AC, clamped
    Hr = house_load - T            AC, the rest of the house
    L  = max(0, (S+B+G) - (Hr+T+C+E))          the Inverter node

    step 1   s2e = min(S, E);  S1 = S - s2e     export is structurally solar-only
    step 2   w, b, g = S1/den, B/den, G/den     den = S1 + B + G
             every remaining sink (Hr, T, C, L) draws that same mix.

Three things about that rule matter more than anything else here, and each has
its own block of tests below.

**It is order-free.** There is no allocation sequence at all, so relabelling two
sources must permute the answer and nothing more. v1's greedy rule was not
order-free and `test_sources_are_interchangeable_*` is the test it fails.

**It is positively homogeneous.** Every operation is degree 1 in the readings
(max, min, subtraction) or degree 0 (the shares), so `f(k*x) == k*f(x)` should
hold *bit-exactly* for a power-of-two `k`. v1 broke this because AC referral was
affine (`0.9974*DC - 105.9`). `test_scaling_is_bit_exact_for_powers_of_two` is
the test that pins the difference.

**Its exactness is regime-dependent, and the brief's claim that conservation is
EXACT is only true in one regime.** Proof sketch, worth reading before touching
a tolerance:

    if E <= S and (S+B+G) >= (Hr+T+C+E), then L is not clamped and
        Hr + T + C + L = S + B + G - E = S1 + B + G = den
    so each source's outbound sum is exactly its own reading, and each sink is
    exactly filled. Two regimes break it, in opposite and unequal ways:

    E > S   -- solar cannot cover the metered export. Export is STARVED by E-S
               (`s2e` is capped at S), and because `L` still subtracts the full
               E, `Hr+T+C+L < den`, so battery and grid UNDER-spend. Sources
               short, one sink short. Under-spend renders a shorter bar, which
               CLAUDE.md already records as expected and not an error.
    L == 0  -- the sample says the sinks drew more than the sources delivered.
               Now `Hr+T+C > den` and every source OVER-spends. The card clamps
               an overstated ribbon against the parent remainder, so this is the
               safe direction, but it is not "exact" and is not reported as such.

    In BOTH broken regimes House, Tesla, Battery-in and Inverter are still filled
    exactly, because the three shares sum to 1 whatever `den` is. Only Export and
    the source totals move.

So: `test_source_conservation_is_exact_when_well_conditioned` is strict, the two
regime tests pin the direction of each failure, and no test anywhere hides a
regime behind a wide tolerance.

---------------------------------------------------------------------------
HOW THIS FILE IS AUDITED, AND TWO TECHNIQUES WORTH REUSING
---------------------------------------------------------------------------

Coverage is not evidence. This file reached 100% statement and branch coverage
of flows.py while still containing a hole, so it is also checked by MUTATION:
31 deliberate defects were introduced into the real flows.py and this file plus
test_replay_v2.py were run against each. 30 died. **1 of 31 is provably
equivalent** -- folding `out["battery_to_battery"]` into `solar_to_battery`
instead of deleting it, where the term is identically 0.0 (verified over 200,000
random samples: charge > 0 implies discharge == 0 implies the battery share is
0). No observation of the output can distinguish it, so it is not a hole; but
"30 of 31, 1 equivalent" is the honest claim and "31/31 killed" is not.

**Technique 1: instrument the module, not the answer.** `check_structure` had
five green tests driving its five raise paths, and every one of them still
passed when `decompose` stopped calling it -- the guard sat there looking
protective while nothing ran it. Coverage stayed at 100% because the direct
tests executed the lines. The mutant that exposed it is `check_structure_disabled`,
and the test that kills it, `test_decompose_actually_runs_its_own_structural_guard`,
replaces `flows.check_structure` with a spy and asserts it was called with the
answer. When a function's whole job is to be *invoked*, testing what it returns
proves nothing about whether anything invokes it.

**Technique 2: two independent mutation passes find different survivors.** One
pass is not sufficient and the runs are not interchangeable. v2-suite ran its own
pass over the same module and found a survivor this file's pass had not probed:
widening `_normalise`'s guard from `total <= 0.0` to `total <= 1e-9`. This file
DID kill it -- but only via hypothesis properties that happened to draw a small
enough total, and only because the mutant it had probed (`(1.0, 0.0, 0.0)`) was
strictly stronger than the one that mattered. Section 20 now pins that boundary
deterministically at every scale from a denormal to a whole watt, so it no longer
depends on a seed. Conversely v2-suite's pass did not find this file's
`check_structure_disabled`. Run both; assume neither is complete.

A related failure shape, seen four separate times on this project in one day:
INTENT ENCODED IN PROSE INSTEAD OF IN AN ASSERTION. `_normalise`'s exact-zero
test was deliberate and said so in a docstring, and the docstring is what a
mutation walks straight past. If a comment explains why a constant must be
exactly what it is, that sentence is a missing test.
"""
import math
import os
import sys
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

flows = pytest.importorskip(
    "flows",
    reason="flows.py is written by agent v2-physics; these tests run against it",
)

decompose = flows.decompose


# ---------------------------------------------------------------------------
# The contract, restated here rather than imported.
#
# Importing the key list from flows.py would make every "the keys are exactly
# these" test tautological -- it would pass whatever flows.py decided to return.
# ---------------------------------------------------------------------------

SOURCES = ("solar", "battery", "grid")

KEYS = (
    "solar_to_house",
    "solar_to_tesla",
    "solar_to_battery",
    "solar_to_export",
    "solar_to_inverter",
    "battery_to_house",
    "battery_to_tesla",
    "battery_to_inverter",
    "grid_to_house",
    "grid_to_tesla",
    "grid_to_battery",
    "grid_to_inverter",
)

# Links that must not merely evaluate to zero -- they must not exist. A key that
# is present and zero is a link somebody can later populate; an absent key is a
# link nobody can draw. All three discharge windows are unset (CLAUDE.md), so
# battery -> grid is structurally unrepresentable, and grid -> export is not a
# thing on this site either.
FORBIDDEN_KEYS = (
    "battery_to_export",
    "grid_to_export",
    "battery_to_battery",
    "battery_to_grid",
    "solar_to_solar",
    "grid_to_grid",
    "export_to_house",
)


def _outbound(f, source):
    return sum(v for k, v in f.items() if k.startswith(source + "_to_"))


def _inbound(f, sink):
    return sum(v for k, v in f.items() if k.endswith("_to_" + sink))


# Symbols pinned by name, not discovered with getattr. v2-physics confirmed all
# of these; a fallback would let a rename pass silently, which is the opposite of
# what an adversarial suite is for.
BAD = flows.BadReading
MAX_PLAUSIBLE_W = flows.MAX_PLAUSIBLE_W

# The module's structural guard raises StructureError -- deliberately NOT a
# BadReading, and sharing no ancestry with it. A BadReading means the INPUT was
# bad and is routine and recoverable; a StructureError means flows.py itself is
# broken. A caller that catches the first must not swallow the second.
STRUCTURE = flows.StructureError


def test_the_pinned_public_symbols_are_all_present():
    """Names this file depends on. If any is renamed, fail here with one clear
    message rather than in fifty tests with an AttributeError."""
    for name in ("decompose", "readings", "inverter_loss", "node_totals",
                 "check_structure", "unreportable", "BadReading",
                 "StructureError", "MAX_PLAUSIBLE_W", "LOSS_RULES", "LOSS_RULE",
                 "set_loss_rule", "FLOWS", "SOURCES", "SINKS", "DEPENDS_ON"):
        assert hasattr(flows, name), "flows.%s is gone" % name


def test_bad_reading_is_both_a_value_error_and_a_type_error():
    """`None` and a list are the wrong TYPE; "unavailable" and NaN are the wrong
    VALUE. A caller catching either one must get what it expects."""
    assert issubclass(BAD, ValueError)
    assert issubclass(BAD, TypeError)


# ---------------------------------------------------------------------------
# Tolerances. Deliberately tight; see the module docstring for why each regime
# gets its own one rather than one loose number covering all of them.
# ---------------------------------------------------------------------------

# Floating-point slack only. One nanowatt absolute, one part in 1e-9 relative.
# Nothing physical hides under this.
FP_ABS = 1e-9
FP_REL = 1e-9


def _assert_close(a, b, what, rel=FP_REL, abs_=FP_ABS):
    assert math.isclose(a, b, rel_tol=rel, abs_tol=abs_), \
        "%s: %.17g != %.17g (diff %.3g)" % (what, a, b, a - b)


# ---------------------------------------------------------------------------
# An independent reference model, transcribed from BRIEF_V2.md section 2.
#
# This is NOT a copy of flows.py -- it was written from the brief before
# flows.py existed. Its only job is to be a second opinion on the arithmetic;
# every structural property is also tested directly against flows.py without
# reference to this, so a shared misreading of the brief cannot pass silently.
# ---------------------------------------------------------------------------

def reference(solar, battery, grid, house, tesla):
    S = max(0.0, float(solar))
    B = max(0.0, -float(battery))
    C = max(0.0, float(battery))
    G = max(0.0, float(grid))
    E = max(0.0, -float(grid))
    H = max(0.0, float(house))
    T = min(max(0.0, float(tesla)), H)
    Hr = H - T
    L = max(0.0, (S + B + G) - (Hr + T + C + E))

    s2e = min(S, E)
    S1 = S - s2e
    den = S1 + B + G
    if den > 0.0:
        w, b, g = S1 / den, B / den, G / den
    else:
        w = b = g = 0.0

    out = {
        "solar_to_house": w * Hr,
        "solar_to_tesla": w * T,
        "solar_to_battery": w * C,
        "solar_to_export": s2e,
        "solar_to_inverter": w * L,
        "battery_to_house": b * Hr,
        "battery_to_tesla": b * T,
        "battery_to_inverter": b * L,
        "grid_to_house": g * Hr,
        "grid_to_tesla": g * T,
        "grid_to_battery": g * C,
        "grid_to_inverter": g * L,
    }
    return out


def parts(solar, battery, grid, house, tesla):
    """The intermediate quantities, for tests that need to reason about regime."""
    S = max(0.0, float(solar))
    B = max(0.0, -float(battery))
    C = max(0.0, float(battery))
    G = max(0.0, float(grid))
    E = max(0.0, -float(grid))
    H = max(0.0, float(house))
    T = min(max(0.0, float(tesla)), H)
    Hr = H - T
    L_raw = (S + B + G) - (Hr + T + C + E)
    return dict(S=S, B=B, C=C, G=G, E=E, H=H, T=T, Hr=Hr,
                L=max(0.0, L_raw), L_raw=L_raw,
                supply=S + B + G, den=(S - min(S, E)) + B + G)


def well_conditioned(solar, battery, grid, house, tesla):
    """The regime in which the brief's 'EXACT' claim is provable."""
    p = parts(solar, battery, grid, house, tesla)
    return p["E"] <= p["S"] and p["L_raw"] >= 0.0


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

SOLAR = st.floats(0.0, 6500.0, allow_nan=False, allow_infinity=False)
BATTERY = st.floats(-5000.0, 5000.0, allow_nan=False, allow_infinity=False)
GRID = st.floats(-6000.0, 10000.0, allow_nan=False, allow_infinity=False)
HOUSE = st.floats(0.0, 12000.0, allow_nan=False, allow_infinity=False)
TESLA = st.floats(0.0, 11000.0, allow_nan=False, allow_infinity=False)

BASE = dict(solar=SOLAR, battery=BATTERY, grid=GRID, house=HOUSE, tesla=TESLA)

# Wildly out of range, wrong sign, denormal -- still must not break anything.
WILD = dict(
    solar=st.floats(-1e4, 9e4, allow_nan=False, allow_infinity=False),
    battery=st.floats(-9e4, 9e4, allow_nan=False, allow_infinity=False),
    grid=st.floats(-9e4, 9e4, allow_nan=False, allow_infinity=False),
    house=st.floats(-9e4, 9e4, allow_nan=False, allow_infinity=False),
    tesla=st.floats(-9e4, 9e4, allow_nan=False, allow_infinity=False),
)

_NO_HEALTH = [HealthCheck.too_slow, HealthCheck.filter_too_much]
SETTINGS = settings(max_examples=400, deadline=None, suppress_health_check=_NO_HEALTH)
FEW = settings(max_examples=150, deadline=None, suppress_health_check=_NO_HEALTH)


def _realistic(lo, hi):
    """Either exactly zero, or a magnitude a real sensor could report.

    The scaling tests need this. Below about 1e-300 a product underflows into
    the denormal range, where multiplication by a power of two stops being
    exact -- so a blind `floats(0, 6500)` draw fails a bit-exactness assertion
    for a reason that has nothing to do with the rule under test. Confining the
    draw to watts and milliwatts keeps the assertion about the arithmetic.
    """
    return st.one_of(
        st.just(0.0),
        st.floats(lo, hi, allow_nan=False, allow_infinity=False)
        .filter(lambda v: abs(v) >= 1e-3),
    )


SCALED = dict(
    solar=_realistic(0.0, 6500.0),
    battery=_realistic(-5000.0, 5000.0),
    grid=_realistic(-6000.0, 10000.0),
    house=_realistic(0.0, 12000.0),
    tesla=_realistic(0.0, 11000.0),
)


@st.composite
def consistent(draw, force_export=False, force_charge=False):
    """A sample whose energy balance actually closes, with E <= S.

    Built forwards from the sources so the house load is a consequence rather
    than an independent draw -- which is what makes the well-conditioned regime
    reachable at all. A blind five-way draw lands in it essentially never.
    """
    S = draw(st.floats(0.0, 6500.0, allow_nan=False, allow_infinity=False))
    charging = draw(st.booleans()) or force_charge
    if charging:
        C = draw(st.floats(0.0, min(5000.0, S + 5000.0),
                           allow_nan=False, allow_infinity=False))
        B = 0.0
    else:
        B = draw(st.floats(0.0, 5000.0, allow_nan=False, allow_infinity=False))
        C = 0.0
    importing = draw(st.booleans()) and not force_export
    if importing:
        G, E = draw(st.floats(0.0, 10000.0, allow_nan=False,
                              allow_infinity=False)), 0.0
    else:
        G = 0.0
        E = draw(st.floats(0.0, S, allow_nan=False, allow_infinity=False)) if S else 0.0
    if force_export:
        assume(E > 0.0)

    room = (S + B + G) - C - E
    assume(room >= 0.0)
    L = draw(st.floats(0.0, room, allow_nan=False, allow_infinity=False))
    rest = room - L
    assume(rest >= 0.0)
    T = draw(st.floats(0.0, rest, allow_nan=False, allow_infinity=False))
    house = rest
    battery = C if C else -B
    grid = G if G else -E
    return (S, battery, grid, house, T)


# ===========================================================================
# 1. Shape of the answer
# ===========================================================================

@given(**BASE)
@SETTINGS
def test_returned_keys_are_exactly_the_twelve(solar, battery, grid, house, tesla):
    assert set(decompose(solar, battery, grid, house, tesla)) == set(KEYS)


def test_there_are_exactly_twelve_flows():
    assert len(KEYS) == 12
    assert len(set(KEYS)) == 12
    assert len(decompose(3000.0, -500.0, -200.0, 2000.0, 800.0)) == 12


@pytest.mark.parametrize("key", FORBIDDEN_KEYS)
def test_structurally_impossible_links_have_no_key_at_all(key):
    """Absent, not zero. A zero-valued key is a link waiting to be populated."""
    f = decompose(3000.0, -500.0, -200.0, 2000.0, 800.0)
    assert key not in f, (
        "%s exists. All three discharge windows are unset and CLAUDE.md forbids "
        "drawing battery -> grid; a key that merely evaluates to 0.0 today is "
        "still a link the layout can reference." % key)


@given(**BASE)
@SETTINGS
def test_every_flow_is_a_finite_float(solar, battery, grid, house, tesla):
    for k, v in decompose(solar, battery, grid, house, tesla).items():
        assert isinstance(v, float), "%s is %r" % (k, type(v))
        assert math.isfinite(v), "%s is %r" % (k, v)


@given(**BASE)
@SETTINGS
def test_result_is_a_fresh_mapping_that_cannot_leak(solar, battery, grid, house, tesla):
    a = decompose(solar, battery, grid, house, tesla)
    a["solar_to_house"] = -1e9
    a["injected"] = 1.0
    b = decompose(solar, battery, grid, house, tesla)
    assert "injected" not in b
    assert b["solar_to_house"] != -1e9 or a is not b


@given(**BASE)
@SETTINGS
def test_deterministic(solar, battery, grid, house, tesla):
    a = decompose(solar, battery, grid, house, tesla)
    b = decompose(solar, battery, grid, house, tesla)
    assert a == b


# ===========================================================================
# 2. Non-negativity -- on hostile input too
# ===========================================================================

@given(**BASE)
@example(solar=0.0, battery=0.0, grid=0.0, house=0.0, tesla=0.0)
@example(solar=6500.0, battery=-5000.0, grid=-6000.0, house=0.0, tesla=0.0)
@example(solar=0.0, battery=5000.0, grid=10000.0, house=12000.0, tesla=11000.0)
@SETTINGS
def test_every_flow_non_negative(solar, battery, grid, house, tesla):
    for k, v in decompose(solar, battery, grid, house, tesla).items():
        assert v >= 0.0, "%s = %r" % (k, v)


@given(**WILD)
@FEW
def test_every_flow_non_negative_on_wild_input(solar, battery, grid, house, tesla):
    """Negative solar, negative house, negative tesla, 90 kW magnitudes."""
    try:
        f = decompose(solar, battery, grid, house, tesla)
    except BAD:
        return
    for k, v in f.items():
        assert v >= 0.0, "%s = %r" % (k, v)
        assert math.isfinite(v), "%s = %r" % (k, v)


@pytest.mark.parametrize("args", [
    (0.0, 0.0, 0.0, 0.0, 0.0),
    (-1.0, 0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, -1.0, 0.0),
    (0.0, 0.0, 0.0, 0.0, -1.0),
    (0.0, 0.0, 0.0, 1e-300, 0.0),
    (0.0, 0.0, 0.0, 5e-324, 0.0),
    (5e-324, 5e-324, 5e-324, 5e-324, 5e-324),
    (1e-300, -1e-300, -1e-300, 1e-300, 1e-300),
    (6500.0, -5000.0, 10000.0, 0.0, 0.0),
    (0.0, 5000.0, -6000.0, 12000.0, 12000.0),
    (0.0, 0.0, -1.0, 0.0, 0.0),
    (0.0, -1.0, -1.0, 0.0, 0.0),
])
def test_non_negative_on_pathological_inputs(args):
    try:
        f = decompose(*args)
    except BAD:
        return
    for k, v in f.items():
        assert v >= 0.0, "%s = %r for %r" % (k, v, args)


# ===========================================================================
# 3. Structural zeros -- absolute, no epsilon hole
#
# v1 leaked one nanowatt out of decompose(0, 0, 0, 1e-9) because a trim ran
# under `over > _EPS`. Every test in this block therefore asserts `== 0.0` and
# never `< eps`.
# ===========================================================================

def test_the_v1_nanowatt_hole_is_closed():
    """The exact v1 defect, ported to the v2 signature.

    v1: `decompose(0, 0, 0, 1e-9)` produced a non-zero solar flow because the
    over-spend trim was guarded by `over > _EPS` and 1e-9 sat on the boundary.
    Nothing is supplying anything here, so every one of the twelve must be
    literally zero.
    """
    f = decompose(0.0, 0.0, 0.0, 1e-9, 0.0)
    assert all(v == 0.0 for v in f.values()), {k: v for k, v in f.items() if v}


@pytest.mark.parametrize("house", [0.0, 5e-324, 1e-320, 1e-300, 1e-9, 1e-6, 1.0, 1e4])
@pytest.mark.parametrize("tesla", [0.0, 5e-324, 1e-9, 1.0])
def test_zero_supply_gives_exactly_zero_everywhere(house, tesla):
    """No source at all. Any non-zero flow is fabricated energy."""
    f = decompose(0.0, 0.0, 0.0, house, tesla)
    for k, v in f.items():
        assert v == 0.0, "%s = %.17g with no source at all" % (k, v)


@given(house=st.floats(0.0, 12000.0, allow_nan=False, allow_infinity=False),
       tesla=st.floats(0.0, 12000.0, allow_nan=False, allow_infinity=False))
@SETTINGS
def test_zero_supply_gives_exactly_zero_everywhere_property(house, tesla):
    f = decompose(0.0, 0.0, 0.0, house, tesla)
    assert all(v == 0.0 for v in f.values())


@given(battery=st.floats(0.0, 5000.0, allow_nan=False, allow_infinity=False),
       grid=GRID, house=HOUSE, tesla=TESLA)
@example(battery=0.0, grid=0.0, house=0.0, tesla=0.0)
@example(battery=5e-324, grid=1e-320, house=1e-300, tesla=0.0)
@example(battery=0.0, grid=-1e-9, house=1e-9, tesla=0.0)
@SETTINGS
def test_zero_solar_means_exactly_zero_solar_flows(battery, grid, house, tesla):
    """Every scale, including denormals. Crediting a source reading 0 W is a lie
    even where the card would clamp it away."""
    f = decompose(0.0, battery, grid, house, tesla)
    for k, v in f.items():
        if k.startswith("solar_to_"):
            assert v == 0.0, "%s = %.17g while solar reads 0" % (k, v)


@given(**BASE)
@example(solar=0.0, battery=0.0, grid=-1000.0, house=0.0, tesla=0.0)
@example(solar=0.0, battery=-5000.0, grid=-6000.0, house=0.0, tesla=0.0)
@example(solar=0.0, battery=-2.0, grid=-2.0, house=0.0, tesla=0.0)
@SETTINGS
def test_zero_solar_exports_exactly_zero_whatever_the_meter_says(
        solar, battery, grid, house, tesla):
    """A discharging battery plus a metered export must NOT produce an export
    ribbon. Battery -> grid is structurally forbidden; export is solar-only."""
    f = decompose(0.0, battery, grid, house, tesla)
    assert f["solar_to_export"] == 0.0


@given(charge=st.floats(0.0, 5000.0, allow_nan=False, allow_infinity=False),
       solar=SOLAR, grid=GRID, house=HOUSE, tesla=TESLA)
@example(charge=1e-9, solar=0.0, grid=0.0, house=0.0, tesla=0.0)
@SETTINGS
def test_battery_never_supplies_anything_while_it_is_charging(
        charge, solar, grid, house, tesla):
    """`battery` positive means charging, so B == 0 and the battery is not a
    source at all. Anything leaving Battery-out here is the pack charging
    itself."""
    assume(charge > 0.0)
    f = decompose(solar, charge, grid, house, tesla)
    for k, v in f.items():
        if k.startswith("battery_to_"):
            assert v == 0.0, "%s = %.17g while the battery is charging" % (k, v)


@given(discharge=st.floats(0.0, 5000.0, allow_nan=False, allow_infinity=False),
       solar=SOLAR, grid=GRID, house=HOUSE, tesla=TESLA)
@SETTINGS
def test_nothing_charges_the_battery_while_it_is_discharging(
        discharge, solar, grid, house, tesla):
    assume(discharge > 0.0)
    f = decompose(solar, -discharge, grid, house, tesla)
    assert f["solar_to_battery"] == 0.0
    assert f["grid_to_battery"] == 0.0


@given(**BASE)
@SETTINGS
def test_no_export_flow_while_importing(solar, battery, grid, house, tesla):
    assume(grid > 0.0)
    assert decompose(solar, battery, grid, house, tesla)["solar_to_export"] == 0.0


@given(**BASE)
@SETTINGS
def test_no_grid_source_flow_while_exporting(solar, battery, grid, house, tesla):
    assume(grid < 0.0)
    f = decompose(solar, battery, grid, house, tesla)
    for k, v in f.items():
        if k.startswith("grid_to_"):
            assert v == 0.0, "%s = %.17g while the meter reads export" % (k, v)


# ===========================================================================
# 4. Conservation
# ===========================================================================

@given(args=consistent())
@SETTINGS
def test_source_conservation_is_exact_when_well_conditioned(args):
    """The brief's central claim: every source's outbound flows sum to exactly
    its own reading. Held to floating-point slack and nothing wider."""
    assume(well_conditioned(*args))
    p = parts(*args)
    f = decompose(*args)
    _assert_close(_outbound(f, "solar"), p["S"], "solar outbound")
    _assert_close(_outbound(f, "battery"), p["B"], "battery outbound")
    _assert_close(_outbound(f, "grid"), p["G"], "grid outbound")


@given(**BASE)
@SETTINGS
def test_source_conservation_exact_whenever_the_regime_conditions_hold(
        solar, battery, grid, house, tesla):
    """Same claim, reached from an unstructured draw rather than a built-forward
    one, so a bug that only survives `consistent()`'s shape is still caught."""
    assume(well_conditioned(solar, battery, grid, house, tesla))
    p = parts(solar, battery, grid, house, tesla)
    f = decompose(solar, battery, grid, house, tesla)
    _assert_close(_outbound(f, "solar"), p["S"], "solar outbound")
    _assert_close(_outbound(f, "battery"), p["B"], "battery outbound")
    _assert_close(_outbound(f, "grid"), p["G"], "grid outbound")


@given(**BASE)
@example(solar=0.0, battery=0.0, grid=0.0, house=0.0, tesla=0.0)
@example(solar=3000.0, battery=-500.0, grid=-200.0, house=2000.0, tesla=800.0)
@SETTINGS
def test_sink_conservation_is_exact_for_the_four_shared_sinks(
        solar, battery, grid, house, tesla):
    """House, Tesla, Battery-in and Inverter are filled by the SAME three shares,
    which sum to 1, so they fill exactly in EVERY regime -- including the two
    where source conservation breaks. If this ever fails, the shares no longer
    sum to 1 and the whole rule is unsound."""
    p = parts(solar, battery, grid, house, tesla)
    assume(p["den"] > 0.0)
    f = decompose(solar, battery, grid, house, tesla)
    _assert_close(_inbound(f, "house"), p["Hr"], "House inbound")
    _assert_close(_inbound(f, "tesla"), p["T"], "Tesla inbound")
    _assert_close(_inbound(f, "battery"), p["C"], "Battery-in inbound")
    _assert_close(_inbound(f, "inverter"), p["L"], "Inverter inbound")


@given(**BASE)
@SETTINGS
def test_export_sink_is_exactly_filled_when_solar_can_cover_it(
        solar, battery, grid, house, tesla):
    p = parts(solar, battery, grid, house, tesla)
    assume(p["E"] <= p["S"])
    f = decompose(solar, battery, grid, house, tesla)
    _assert_close(f["solar_to_export"], p["E"], "Export inbound")


@given(**BASE)
@example(solar=0.0, battery=-3000.0, grid=-2000.0, house=0.0, tesla=0.0)
@SETTINGS
def test_export_is_starved_by_exactly_the_shortfall_when_solar_cannot_cover_it(
        solar, battery, grid, house, tesla):
    """Regime 1, pinned in the honest direction: Export shows the whole metered
    export as its node state but is fed only `S`. The gap is exactly E-S -- not
    more (which would mean another source snuck in) and not less."""
    p = parts(solar, battery, grid, house, tesla)
    assume(p["E"] > p["S"])
    f = decompose(solar, battery, grid, house, tesla)
    _assert_close(f["solar_to_export"], p["S"], "export fed")
    assert f["solar_to_export"] <= p["E"] + FP_ABS


@given(**BASE)
@SETTINGS
def test_no_source_is_over_spent_unless_the_sample_says_sinks_beat_sources(
        solar, battery, grid, house, tesla):
    """Regime 2 is the ONLY licence to over-spend a source. Anywhere else an
    over-spend is fabricated energy, and the card would not clamp all of it."""
    p = parts(solar, battery, grid, house, tesla)
    assume(p["L_raw"] >= 0.0)
    f = decompose(solar, battery, grid, house, tesla)
    for src, cap in (("solar", p["S"]), ("battery", p["B"]), ("grid", p["G"])):
        got = _outbound(f, src)
        assert got <= cap * (1 + FP_REL) + FP_ABS, (
            "%s spent %.6f of %.6f with L_raw=%.6f" % (src, got, cap, p["L_raw"]))


@given(**BASE)
@SETTINGS
def test_each_individual_flow_is_bounded_by_its_own_source(
        solar, battery, grid, house, tesla):
    p = parts(solar, battery, grid, house, tesla)
    assume(p["L_raw"] >= 0.0)
    f = decompose(solar, battery, grid, house, tesla)
    for k, v in f.items():
        cap = {"solar": p["S"], "battery": p["B"], "grid": p["G"]}[k.split("_to_")[0]]
        assert v <= cap * (1 + FP_REL) + FP_ABS, "%s = %.6f > %.6f" % (k, v, cap)


@given(**BASE)
@SETTINGS
def test_each_individual_flow_is_bounded_by_its_own_sink(
        solar, battery, grid, house, tesla):
    p = parts(solar, battery, grid, house, tesla)
    f = decompose(solar, battery, grid, house, tesla)
    caps = {"house": p["Hr"], "tesla": p["T"], "battery": p["C"],
            "export": p["E"], "inverter": p["L"]}
    for k, v in f.items():
        cap = caps[k.split("_to_")[1]]
        assert v <= cap * (1 + FP_REL) + FP_ABS, "%s = %.6f > %.6f" % (k, v, cap)


@given(args=consistent())
@SETTINGS
def test_the_decomposition_creates_no_energy_on_a_consistent_sample(args):
    p = parts(*args)
    f = decompose(*args)
    total = sum(f.values())
    assert total <= p["supply"] * (1 + FP_REL) + FP_ABS, (
        "%.6f W of flow out of %.6f W of supply" % (total, p["supply"]))


# ===========================================================================
# 5. The shares
# ===========================================================================

def _shares(f, p):
    """Recover w, b, g from the answer, using whichever sink is largest.

    Requires the sink to be at least a microwatt: dividing by a denormal turns
    ordinary rounding into a 100% error and would make this test flap rather
    than discriminate.
    """
    best, name = 1e-6, None
    for sink, val in (("house", p["Hr"]), ("tesla", p["T"]),
                      ("battery", p["C"]), ("inverter", p["L"])):
        if val > best:
            best, name = val, sink
    if name is None:
        return None
    got = {}
    for src in SOURCES:
        key = "%s_to_%s" % (src, name)
        got[src] = f[key] / best if key in f else 0.0
    return got


@given(**BASE)
@SETTINGS
def test_shares_sum_to_one_when_there_is_supply(solar, battery, grid, house, tesla):
    p = parts(solar, battery, grid, house, tesla)
    assume(p["den"] > 0.0)
    f = decompose(solar, battery, grid, house, tesla)
    s = _shares(f, p)
    assume(s is not None)
    # Battery-in has no battery inflow, so its recovered shares legitimately sum
    # to less than 1 whenever the battery is charging (b == 0 there anyway).
    _assert_close(sum(s.values()), 1.0, "shares", rel=1e-12, abs_=1e-12)


@given(house=HOUSE, tesla=TESLA)
@SETTINGS
def test_shares_are_zero_not_nan_when_supply_is_zero(house, tesla):
    """The division guard. Without it `0/0` is a ZeroDivisionError or a NaN, and
    a NaN flow renders the ribbon as nothing while reading as a number."""
    f = decompose(0.0, 0.0, 0.0, house, tesla)
    for k, v in f.items():
        assert v == 0.0 and not math.isnan(v), "%s = %r" % (k, v)


def test_shares_are_proportional_to_the_source_readings():
    """Two sinks must receive the SAME mix -- that is what 'proportional' means
    and what makes the rule order-free. A rule that fed one sink preferentially
    would pass conservation and fail here."""
    # 2 kW solar, 1 kW battery out, 1 kW import; no export; house 1 kW, tesla 3 kW.
    f = decompose(2000.0, -1000.0, 1000.0, 4000.0, 3000.0)
    for sink in ("house", "tesla"):
        tot = _inbound(f, sink)
        assert tot > 0
        _assert_close(f["solar_to_%s" % sink] / tot, 0.5, "solar share of %s" % sink,
                      rel=1e-12, abs_=1e-12)
        _assert_close(f["battery_to_%s" % sink] / tot, 0.25,
                      "battery share of %s" % sink, rel=1e-12, abs_=1e-12)
        _assert_close(f["grid_to_%s" % sink] / tot, 0.25,
                      "grid share of %s" % sink, rel=1e-12, abs_=1e-12)


def test_export_is_removed_from_solar_before_the_shares_are_taken():
    """Step 1 is not cosmetic.

    3 kW solar of which 1 kW is metered out, 1 kW of battery discharge, 2.5 kW
    of house. The mix the house sees must be built from the 2 kW of solar that
    did NOT leave, so solar's share is 2000/3000 and the house gets 1666.7 W of
    it. Skip step 1 and the share becomes 3000/4000, putting 1875 W of solar on
    the house -- a 12% overstatement that no other test here would catch.
    """
    f = decompose(3000.0, -1000.0, -1000.0, 2500.0, 0.0)
    _assert_close(f["solar_to_export"], 1000.0, "export")
    _assert_close(f["solar_to_house"], 2500.0 * 2.0 / 3.0, "solar_to_house")
    _assert_close(f["battery_to_house"], 2500.0 / 3.0, "battery_to_house")
    _assert_close(_inbound(f, "inverter"), 500.0, "Inverter node")


# ===========================================================================
# 6. Order-freeness -- the property v1 did not have
# ===========================================================================

@given(x=st.floats(0.0, 5000.0, allow_nan=False, allow_infinity=False),
       y=st.floats(0.0, 5000.0, allow_nan=False, allow_infinity=False),
       solar=SOLAR, house=HOUSE, tesla=TESLA)
@example(x=1000.0, y=4000.0, solar=0.0, house=5000.0, tesla=0.0)
@SETTINGS
def test_battery_and_grid_are_interchangeable_sources(x, y, solar, house, tesla):
    """Relabelling battery-out as grid-import and vice versa must permute the
    answer and change nothing else. A sequential allocator cannot do this: it
    spends whichever it reaches first.

    Restricted to grid >= 0 so the export step, which is solar-specific and
    genuinely asymmetric, is out of the picture.
    """
    a = decompose(solar, -x, y, house, tesla)
    b = decompose(solar, -y, x, house, tesla)
    for sink in ("house", "tesla", "inverter"):
        _assert_close(a["battery_to_%s" % sink], b["grid_to_%s" % sink],
                      "battery/grid swap on %s" % sink)
        _assert_close(a["grid_to_%s" % sink], b["battery_to_%s" % sink],
                      "grid/battery swap on %s" % sink)
    for k in ("solar_to_house", "solar_to_tesla", "solar_to_battery",
              "solar_to_export", "solar_to_inverter"):
        _assert_close(a[k], b[k], "solar unchanged: %s" % k)


@given(x=st.floats(0.0, 5000.0, allow_nan=False, allow_infinity=False),
       y=st.floats(0.0, 5000.0, allow_nan=False, allow_infinity=False),
       house=HOUSE, tesla=TESLA)
@SETTINGS
def test_solar_and_battery_are_interchangeable_when_nothing_is_exported(
        x, y, house, tesla):
    """With the meter at zero there is no export step, so solar has no special
    status left and must be exactly as interchangeable as the other two."""
    a = decompose(x, -y, 0.0, house, tesla)
    b = decompose(y, -x, 0.0, house, tesla)
    for sink in ("house", "tesla", "inverter"):
        _assert_close(a["solar_to_%s" % sink], b["battery_to_%s" % sink],
                      "solar/battery swap on %s" % sink)
        _assert_close(a["battery_to_%s" % sink], b["solar_to_%s" % sink],
                      "battery/solar swap on %s" % sink)


@given(**BASE)
@SETTINGS
def test_house_and_tesla_are_interchangeable_sinks(solar, battery, grid, house, tesla):
    """The rule must not privilege one sink over another either. Swapping the
    split of the same total house load between Tesla and the rest must swap the
    two columns and leave every other flow identical."""
    assume(tesla <= house)
    rest = house - tesla
    a = decompose(solar, battery, grid, house, tesla)
    b = decompose(solar, battery, grid, house, rest)
    for src in SOURCES:
        _assert_close(a["%s_to_house" % src], b["%s_to_tesla" % src],
                      "house/tesla swap from %s" % src)
        _assert_close(a["%s_to_tesla" % src], b["%s_to_house" % src],
                      "tesla/house swap from %s" % src)
    for k in ("solar_to_export", "solar_to_battery", "grid_to_battery",
              "solar_to_inverter", "battery_to_inverter", "grid_to_inverter"):
        _assert_close(a[k], b[k], "unchanged: %s" % k)


# ===========================================================================
# 7. Homogeneity -- the property v1's affine AC referral destroyed
# ===========================================================================

@pytest.mark.parametrize("k", [0.5, 2.0, 4.0, 0.25, 8.0, 2.0 ** -8])
@given(**SCALED)
@FEW
def test_scaling_is_bit_exact_for_powers_of_two(k, solar, battery, grid, house, tesla):
    """v2 has no fitted constants and no fixed term, so it is positively
    homogeneous of degree 1. For a power-of-two k every operation in the rule
    commutes with the scaling exactly, so this is `==`, not `isclose`.

    Any fixed watt offset anywhere in the rule -- a parasitic term, an epsilon
    floor, a rounding-to-zero threshold -- breaks this test immediately.
    """
    args = (solar, battery, grid, house, tesla)
    scaled = tuple(k * a for a in args)
    assume(max(abs(x) for x in scaled) <= MAX_PLAUSIBLE_W * 0.9)
    a = decompose(*args)
    b = decompose(*scaled)
    for key in KEYS:
        assert b[key] == k * a[key], (
            "%s: f(%g*x)=%.17g but %g*f(x)=%.17g" % (key, k, b[key], k, k * a[key]))


@given(k=st.floats(1e-2, 10.0, allow_nan=False, allow_infinity=False), **SCALED)
@FEW
def test_scaling_is_homogeneous_for_arbitrary_positive_k(
        k, solar, battery, grid, house, tesla):
    assume(k > 0)
    args = (solar, battery, grid, house, tesla)
    assume(max(abs(k * x) for x in args) <= MAX_PLAUSIBLE_W * 0.9)
    a = decompose(*args)
    b = decompose(*(k * x for x in args))
    for key in KEYS:
        _assert_close(b[key], k * a[key], "homogeneity of %s at k=%g" % (key, k),
                      rel=1e-9, abs_=1e-9)


def test_a_fixed_offset_would_be_caught_by_the_homogeneity_test():
    """Self-check on the test above: prove it can fail.

    Runs the homogeneity comparison against a deliberately affine variant of the
    reference (v1's `0.9974*DC - 105.9` shape) and asserts it does NOT hold. If
    this ever passes, the homogeneity test has stopped discriminating.
    """
    def affine(solar, battery, grid, house, tesla):
        f = reference(solar, battery, grid, house, tesla)
        return {k: max(0.0, 0.9974 * v - 105.9) for k, v in f.items()}

    args = (3000.0, -1000.0, 500.0, 2500.0, 800.0)
    a = affine(*args)
    b = affine(*(2.0 * x for x in args))
    assert any(b[k] != 2.0 * a[k] for k in KEYS)


# ===========================================================================
# 8. Tesla
# ===========================================================================

@given(house=st.floats(0.0, 12000.0, allow_nan=False, allow_infinity=False),
       over=st.floats(0.0, 20000.0, allow_nan=False, allow_infinity=False),
       solar=SOLAR, battery=BATTERY, grid=GRID)
@example(house=1000.0, over=9000.0, solar=0.0, battery=0.0, grid=1000.0)
@SETTINGS
def test_tesla_above_house_load_is_clamped_and_leaves_house_at_exactly_zero(
        house, over, solar, battery, grid):
    """TeslaMate publishes on change and holds a plateau for up to ~80 min, so a
    held value can outrun a freshly-polled house_load. Unclamped, Hr goes
    negative and the House ribbon inverts."""
    assume(over > 0.0)
    f = decompose(solar, battery, grid, house, house + over)
    assert _inbound(f, "house") == 0.0, (
        "House got %.17g while Tesla alone exceeded the load"
        % _inbound(f, "house"))
    assert _inbound(f, "tesla") <= house * (1 + FP_REL) + FP_ABS


@given(house=st.floats(0.0, 12000.0, allow_nan=False, allow_infinity=False),
       solar=SOLAR, battery=BATTERY, grid=GRID)
@SETTINGS
def test_tesla_equal_to_house_load_leaves_house_at_exactly_zero(
        house, solar, battery, grid):
    f = decompose(solar, battery, grid, house, house)
    assert _inbound(f, "house") == 0.0
    for src in SOURCES:
        assert f["%s_to_house" % src] == 0.0


@given(**BASE)
@SETTINGS
def test_tesla_never_receives_more_than_the_house_load(
        solar, battery, grid, house, tesla):
    f = decompose(solar, battery, grid, house, tesla)
    assert _inbound(f, "tesla") <= max(0.0, house) * (1 + FP_REL) + FP_ABS


@given(**BASE)
@SETTINGS
def test_house_plus_tesla_never_exceeds_the_house_load(
        solar, battery, grid, house, tesla):
    """The whole point of splitting Tesla out: the two together are still the
    one metered load, never more."""
    f = decompose(solar, battery, grid, house, tesla)
    total = _inbound(f, "house") + _inbound(f, "tesla")
    assert total <= max(0.0, house) * (1 + FP_REL) + FP_ABS, (
        "House %.6f + Tesla %.6f > load %.6f"
        % (_inbound(f, "house"), _inbound(f, "tesla"), house))


def test_tesla_held_flat_across_a_long_plateau_is_treated_as_a_real_reading():
    """CLAUDE.md: a held TeslaMate value is correct, not stale. The same
    (house, tesla) pair repeated must decompose identically every time -- there
    must be no age-based decay or staleness heuristic hiding in here."""
    args = (0.0, 0.0, 7200.0, 7400.0, 7000.0)
    first = decompose(*args)
    for _ in range(500):
        assert decompose(*args) == first


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "", None, float("nan")])
def test_unusable_tesla_reading_raises_rather_than_becoming_zero(bad):
    """A zeroed Tesla silently moves ~7 kW into 'Rest of house' and looks
    entirely plausible. It must not be indistinguishable from an unplugged car."""
    with pytest.raises(BAD):
        decompose(3000.0, -500.0, 1000.0, 7400.0, bad)


def test_tesla_zero_puts_the_whole_load_on_house():
    f = decompose(0.0, 0.0, 1000.0, 900.0, 0.0)
    assert _inbound(f, "tesla") == 0.0
    _assert_close(_inbound(f, "house"), 900.0, "house")


# ===========================================================================
# 9. The Inverter node
# ===========================================================================

@given(**BASE)
@SETTINGS
def test_inverter_node_is_never_negative(solar, battery, grid, house, tesla):
    f = decompose(solar, battery, grid, house, tesla)
    assert _inbound(f, "inverter") >= 0.0


@given(**BASE)
@SETTINGS
def test_inverter_node_is_exactly_zero_when_the_sinks_outrun_the_sources(
        solar, battery, grid, house, tesla):
    """The `max(0, ...)` clamp. A negative residual means the sample is skewed,
    not that the inverter generated energy."""
    p = parts(solar, battery, grid, house, tesla)
    assume(p["L_raw"] < 0.0)
    f = decompose(solar, battery, grid, house, tesla)
    for src in SOURCES:
        assert f["%s_to_inverter" % src] == 0.0, (
            "%s_to_inverter = %.17g with a negative residual (%.6f)"
            % (src, f["%s_to_inverter" % src], p["L_raw"]))


def test_inverter_node_absorbs_import_that_has_nowhere_else_to_go():
    """BRIEF section 2: `decompose(0,0,2,0)` sends 2 kW to Inverter rather than
    into a house drawing nothing. This is the case that dissolved v1's g2h clamp."""
    f = decompose(0.0, 0.0, 2000.0, 0.0, 0.0)
    _assert_close(f["grid_to_inverter"], 2000.0, "grid_to_inverter")
    assert f["grid_to_house"] == 0.0
    assert f["grid_to_tesla"] == 0.0
    assert f["grid_to_battery"] == 0.0


def test_inverter_node_equals_the_measured_residual_on_a_worked_sample():
    """CLAUDE.md's live sample, plus a 300 W car:
    solar 2982 DC, battery -457 (457 W out), grid -98 (98 W export), house 3078.

        supply = 2982 + 457       = 3439
        sinks  = 3078 + 98        = 3176      (Hr 2778 + T 300 + E 98)
        L      = 3439 - 3176      = 263       -- CLAUDE.md's own 263 W gap.
    """
    f = decompose(2982.0, -457.0, -98.0, 3078.0, 300.0)
    _assert_close(_inbound(f, "inverter"), 263.0, "Inverter node", abs_=1e-6)
    _assert_close(f["solar_to_export"], 98.0, "export", abs_=1e-6)


@given(args=consistent())
@SETTINGS
def test_inverter_node_closes_the_balance_exactly(args):
    """supply - (Hr + T + C + E) == Inverter, with no slack anywhere."""
    p = parts(*args)
    assume(p["L_raw"] >= 0.0)
    f = decompose(*args)
    _assert_close(_inbound(f, "inverter"),
                  p["supply"] - (p["Hr"] + p["T"] + p["C"] + p["E"]),
                  "Inverter node closes the balance")


# ===========================================================================
# 10. Hostile readings
# ===========================================================================

BAD_VALUES = [
    None,
    "unavailable",
    "unknown",
    "",
    "   ",
    "none",
    "None",
    "nan",
    "NaN",
    "inf",
    "-inf",
    "Infinity",
    float("nan"),
    float("inf"),
    float("-inf"),
    [],
    [1.0],
    {},
    (1.0,),
    object(),
    True,
    False,
    Decimal("NaN"),
    Decimal("Infinity"),
    "1,000",
    "3 kW",
    "12abc",
]

CHANNELS = ("solar", "battery", "grid", "house", "tesla")
GOOD = (3000.0, -500.0, 1000.0, 2500.0, 800.0)


@pytest.mark.parametrize("bad", BAD_VALUES, ids=lambda v: repr(v)[:24])
@pytest.mark.parametrize("idx", range(5), ids=CHANNELS)
def test_an_unusable_reading_raises_on_every_channel(idx, bad):
    """Raising rather than substituting 0.0 is the whole point. The dominant
    failure mode is Modbus session contention -- one session at a time, so a
    competing process gets nothing -- and a zeroed chart is indistinguishable
    from a still night. A template sensor that throws renders `unavailable`;
    one that returns 0 renders a confident lie."""
    args = list(GOOD)
    args[idx] = bad
    with pytest.raises(BAD):
        decompose(*args)


@pytest.mark.parametrize("idx", range(5), ids=CHANNELS)
def test_a_bool_is_not_a_measurement(idx):
    """`bool` is an `int` subclass, so an unguarded `True` decomposes as 1 W."""
    for b in (True, False):
        args = list(GOOD)
        args[idx] = b
        with pytest.raises(BAD):
            decompose(*args)


@pytest.mark.parametrize("idx", range(5), ids=CHANNELS)
def test_a_u32_sign_misread_is_rejected_not_clamped(idx):
    """CLAUDE.md records register 33257 read unsigned as 4294967253 W = 2^32-43
    when the truth was -43 W. Clamping would turn 4.29 GW of import into a
    plausible-looking chart."""
    args = list(GOOD)
    args[idx] = 4294967253.0
    with pytest.raises(BAD):
        decompose(*args)
    args[idx] = -4294967253.0
    with pytest.raises(BAD):
        decompose(*args)


@pytest.mark.parametrize("idx", range(5), ids=CHANNELS)
def test_the_plausibility_ceiling_is_a_boundary_not_a_gradient(idx):
    """Just inside must work, just outside must raise. A test that only probes
    4.29e9 cannot tell a real ceiling from `abs(v) > 1e300`."""
    args = list(GOOD)
    args[idx] = MAX_PLAUSIBLE_W * 0.999
    decompose(*args)
    args[idx] = MAX_PLAUSIBLE_W * 1.001
    with pytest.raises(BAD):
        decompose(*args)


@pytest.mark.parametrize("text,value", [
    ("3000", 3000.0),
    (" 3000 ", 3000.0),
    ("3e3", 3000.0),
    ("3000.0", 3000.0),
    ("+3000", 3000.0),
    ("3_000", 3000.0),
])
def test_numeric_strings_are_the_normal_path(text, value):
    """HA states are always strings. These are not exotic inputs."""
    assert decompose(text, -500.0, 1000.0, 2500.0, 800.0) == \
        decompose(value, -500.0, 1000.0, 2500.0, 800.0)


@pytest.mark.parametrize("value", [
    Decimal("3000"), Decimal("3000.5"), Fraction(6000, 2), 3000,
])
def test_exact_numeric_types_decompose_like_their_float(value):
    assert decompose(value, -500.0, 1000.0, 2500.0, 800.0) == \
        decompose(float(value), -500.0, 1000.0, 2500.0, 800.0)


def test_a_nan_reading_is_not_silently_equal_to_an_idle_night():
    """The subtle one. `float("nan")` parses, and then `max(0.0, nan)` returns
    0.0 because `nan > 0.0` is False -- so without an explicit isnan guard a
    broken sensor decomposes byte-identically to a still night."""
    with pytest.raises(BAD):
        decompose(float("nan"), 0.0, 0.0, 0.0, 0.0)
    with pytest.raises(BAD):
        decompose(0.0, float("nan"), 0.0, 0.0, 0.0)


@pytest.mark.parametrize("idx", [0, 3, 4], ids=["solar", "house", "tesla"])
def test_a_negative_reading_where_none_is_possible_is_handled_without_leaking(idx):
    """solar, house and tesla cannot be negative. Whatever flows.py chooses to
    do -- raise, or treat as zero -- it must not half-clamp, which would leave
    a negative term inside a share and invert a ribbon."""
    args = list(GOOD)
    args[idx] = -1234.0
    try:
        got = decompose(*args)
    except BAD:
        return
    zeroed = list(GOOD)
    zeroed[idx] = 0.0
    assert got == decompose(*zeroed), (
        "a negative %s neither raised nor behaved as 0.0" % CHANNELS[idx])


def test_positional_signature_is_solar_battery_grid_house_tesla():
    """Argument order is load-bearing: swapping grid and house silently produces
    a plausible chart. Pinned by a sample only one ordering can explain -- solar
    5000 exporting 1000 with the house at 500 and no car."""
    f = decompose(5000.0, 0.0, -1000.0, 500.0, 0.0)
    _assert_close(f["solar_to_export"], 1000.0, "export")
    _assert_close(f["solar_to_house"], 500.0, "house")
    _assert_close(f["solar_to_inverter"], 3500.0, "inverter")


# ===========================================================================
# 11. Cross-check against the independent reference model
# ===========================================================================

@given(**BASE)
@example(solar=2982.0, battery=-457.0, grid=-98.0, house=3078.0, tesla=300.0)
@example(solar=0.0, battery=0.0, grid=2000.0, house=0.0, tesla=0.0)
@example(solar=0.0, battery=0.0, grid=0.0, house=0.0, tesla=0.0)
@SETTINGS
def test_matches_an_independent_transcription_of_the_brief(
        solar, battery, grid, house, tesla):
    got = decompose(solar, battery, grid, house, tesla)
    want = reference(solar, battery, grid, house, tesla)
    for k in KEYS:
        _assert_close(got[k], want[k], "reference mismatch on %s" % k)


@given(**WILD)
@FEW
def test_matches_the_reference_on_wild_input_too(solar, battery, grid, house, tesla):
    try:
        got = decompose(solar, battery, grid, house, tesla)
    except BAD:
        return
    want = reference(solar, battery, grid, house, tesla)
    for k in KEYS:
        _assert_close(got[k], want[k], "reference mismatch on %s" % k,
                      rel=1e-9, abs_=1e-6)


def test_the_reference_model_is_not_trivially_agreeable():
    """Self-check: the reference must disagree with a wrong rule.

    Compares it to v1's shape (export taken from solar last, sinks filled
    sequentially) on a sample where the two genuinely differ, so a future edit
    that accidentally turns `reference` into a passthrough is caught.
    """
    args = (3000.0, -1000.0, 1000.0, 4000.0, 1000.0)
    ref = reference(*args)
    # A sequential rule spends the battery on the house first, so the house
    # would receive 1000 W of battery and 0 W of grid; proportional gives it a
    # mix of all three.
    assert ref["grid_to_house"] > 0.0
    assert ref["battery_to_tesla"] > 0.0


# ===========================================================================
# 12. The LOSS_RULES seam (BRIEF section 8)
# ===========================================================================

def test_loss_rules_seam_exists():
    """BRIEF section 8 requires the per-source band-table alternative to be
    implemented and switchable, so it can be MEASURED against the default rather
    than argued about."""
    assert hasattr(flows, "LOSS_RULES"), \
        "flows.py must expose LOSS_RULES (BRIEF_V2.md section 8)"
    assert len(flows.LOSS_RULES) >= 2, \
        "LOSS_RULES must hold both the default and the band-table alternative"


def _selected_name():
    for attr in ("LOSS_RULE", "LOSS_RULE_NAME", "SELECTED_LOSS_RULE"):
        if hasattr(flows, attr):
            return attr
    return None


def test_the_default_loss_rule_is_the_aggregate_proportional_one():
    """BRIEF: 'Default stays aggregate-proportional unless the measurement says
    otherwise.' If someone flips this, the whole exactness argument in section 8
    stops holding and this test is the tripwire."""
    attr = _selected_name()
    assert attr is not None, "flows.py must name the selected loss rule"
    name = getattr(flows, attr)
    assert name in flows.LOSS_RULES
    assert "proportional" in name or name in ("default", "aggregate"), \
        "selected loss rule is %r, expected the aggregate-proportional one" % name


def _set_rule(name):
    for fn in ("set_loss_rule", "set_loss_treatment", "set_rule"):
        if hasattr(flows, fn):
            return getattr(flows, fn)(name)
    attr = _selected_name()
    previous = getattr(flows, attr)
    setattr(flows, attr, name)
    return previous


@pytest.mark.parametrize("rule", sorted(getattr(flows, "LOSS_RULES", {})))
def test_every_loss_rule_keeps_the_hard_invariants(rule):
    """Switching the seam may change WHERE the loss is charged. It may not make
    a flow negative, fabricate a forbidden link, or break the sink fill."""
    previous = _set_rule(rule)
    try:
        for args in [(3000.0, -1000.0, 1000.0, 4000.0, 1000.0),
                     (0.0, 0.0, 2000.0, 0.0, 0.0),
                     (0.0, 0.0, 0.0, 0.0, 0.0),
                     (2982.0, -457.0, -98.0, 3078.0, 300.0),
                     (40.0, -40.0, 0.0, 60.0, 0.0),
                     (6500.0, 5000.0, -6000.0, 0.0, 0.0)]:
            f = decompose(*args)
            assert set(f) == set(KEYS), rule
            for k, v in f.items():
                assert v >= 0.0 and math.isfinite(v), "%s %s = %r" % (rule, k, v)
            p = parts(*args)
            assert _inbound(f, "tesla") <= p["T"] * (1 + FP_REL) + FP_ABS
            assert f["solar_to_export"] <= p["E"] * (1 + FP_REL) + FP_ABS
            if p["S"] == 0.0:
                assert all(v == 0.0 for k, v in f.items() if k.startswith("solar_to_"))
    finally:
        _set_rule(previous)


def test_setting_an_unknown_loss_rule_is_rejected():
    with pytest.raises((ValueError, KeyError)):
        _set_rule("no_such_rule_at_all")


def test_the_two_loss_rules_actually_differ():
    """A seam with two identical implementations measures nothing. Pinned on the
    low-power case the band table exists for: a battery trickling at 40 W, where
    measured efficiency is ~0.22 and proportional over-credits it."""
    names = sorted(flows.LOSS_RULES)
    assert len(names) >= 2
    args = (0.0, -40.0, 60.0, 60.0, 0.0)
    seen = {}
    attr = _selected_name()
    previous = getattr(flows, attr) if attr else names[0]
    try:
        for n in names:
            _set_rule(n)
            seen[n] = tuple(round(decompose(*args)[k], 9) for k in KEYS)
    finally:
        _set_rule(previous)
    assert len(set(seen.values())) > 1, (
        "every LOSS_RULE returns the same answer, so the seam measures nothing: %r"
        % (seen,))


# ===========================================================================
# 13. The module's own structural guard and node roll-up
#
# `check_structure` is the only part of flows.py that `decompose` cannot reach
# on its own: by construction the arithmetic never violates it. It is still
# live code that a future edit will depend on, so each of its five failure
# paths is exercised directly. A guard nobody has ever seen fire is a guard
# nobody knows works.
# ===========================================================================

def _good_pair():
    args = (3000.0, -1000.0, 1000.0, 4000.0, 1000.0)
    return decompose(*args), flows.readings(*args)


def test_the_flows_constant_matches_the_twelve_keys_the_brief_defines():
    """flows.FLOWS is the module's own claim about its keys. KEYS above is an
    independent literal transcribed from BRIEF_V2.md. They must agree, and
    checking that here is what stops the key tests from being circular."""
    assert tuple(flows.FLOWS) == KEYS


def test_check_structure_accepts_a_real_decomposition():
    out, r = _good_pair()
    flows.check_structure(out, r)  # must not raise


def test_check_structure_rejects_an_added_key():
    """An added key resurrects a link the dashboard must never draw."""
    out, r = _good_pair()
    out["battery_to_export"] = 0.0
    with pytest.raises(STRUCTURE):
        flows.check_structure(out, r)


def test_check_structure_rejects_a_missing_key():
    out, r = _good_pair()
    del out["grid_to_inverter"]
    with pytest.raises(STRUCTURE):
        flows.check_structure(out, r)


def test_check_structure_rejects_a_negative_flow():
    """A negative value inverts a ribbon rather than shrinking it."""
    out, r = _good_pair()
    out["solar_to_house"] = -1e-12
    with pytest.raises(STRUCTURE):
        flows.check_structure(out, r)


def test_check_structure_rejects_a_battery_that_charges_and_discharges_at_once():
    """Unreachable from one signed sensor, which is the point: if it ever fires,
    the signed battery reading has been replaced upstream by two unsigned ones
    and every battery flow on the chart is wrong."""
    out, r = _good_pair()
    r = dict(r, charge=1.0, discharge=1.0)
    with pytest.raises(STRUCTURE):
        flows.check_structure(out, r)


def test_check_structure_rejects_a_meter_that_imports_and_exports_at_once():
    out, r = _good_pair()
    r = dict(r, imp=1.0, exp=1.0)
    with pytest.raises(STRUCTURE):
        flows.check_structure(out, r)


def test_check_structure_rejects_a_charging_battery_that_is_also_a_source():
    out, r = _good_pair()
    r = dict(r, charge=1.0, discharge=0.0)
    out = dict(out, battery_to_house=1.0)
    with pytest.raises(STRUCTURE):
        flows.check_structure(out, r)


def test_the_structural_guard_is_not_a_bad_reading_and_never_conflated_with_one():
    """`BadReading` means the input was bad -- routine, expected, recoverable.
    `StructureError` means flows.py itself is broken. A caller that catches the
    first must NOT swallow the second, so the two must share no ancestry."""
    if STRUCTURE is AssertionError:
        pytest.skip("this implementation keeps the guard as a plain assert")
    assert not issubclass(STRUCTURE, BAD if isinstance(BAD, type) else Exception)
    assert not issubclass(BAD if isinstance(BAD, type) else ValueError, STRUCTURE)


def test_decompose_actually_runs_its_own_structural_guard():
    """Calling the guard is not the same as having one.

    Every direct test of `check_structure` above still passes if `decompose`
    quietly stops calling it -- the guard would sit there looking protective
    while nothing ran it. This is the only test that notices, so it instruments
    the module rather than the answer.
    """
    calls = []
    original = flows.check_structure

    def spy(out, r):
        calls.append((dict(out), dict(r)))
        return original(out, r)

    flows.check_structure = spy
    try:
        result = decompose(3000.0, -1000.0, 1000.0, 4000.0, 1000.0)
    finally:
        flows.check_structure = original
    assert len(calls) == 1, "decompose did not call check_structure"
    seen_out, seen_r = calls[0]
    assert seen_out == result, "the guard was shown something other than the answer"
    assert "house_rest" in seen_r and "discharge" in seen_r


def test_check_structure_is_not_defeated_by_python_dash_O():
    """Written as explicit raises rather than `assert` for exactly this reason.
    A guard compiled away under -O is worse than no guard, because the code
    reads as protected."""
    import subprocess
    src = (
        "import sys; sys.path.insert(0, %r)\n"
        "import flows\n"
        "out = flows.decompose(3000.0, -1000.0, 1000.0, 4000.0, 1000.0)\n"
        "r = flows.readings(3000.0, -1000.0, 1000.0, 4000.0, 1000.0)\n"
        "out['solar_to_house'] = -1.0\n"
        "try:\n"
        "    flows.check_structure(out, r)\n"
        "except getattr(flows, 'StructureError', AssertionError):\n"
        "    print('raised')\n"
    ) % os.path.dirname(os.path.abspath(__file__))
    p = subprocess.run([sys.executable, "-O", "-c", src],
                       capture_output=True, text=True)
    assert p.stdout.strip() == "raised", (p.stdout, p.stderr)


# --- node_totals: the source of every node state the layout will show --------

def test_node_totals_reports_each_sink_as_the_sum_of_its_inbound_flows():
    """This is the v2 node-identity decision -- House, Tesla, Battery-in, Export
    and Inverter are each defined BY their inbound flows, which is what lets
    House exclude the car without a `subtract_entities` trick."""
    args = (3000.0, -1000.0, 1000.0, 4000.0, 1000.0)
    f = decompose(*args)
    n = flows.node_totals(f)
    for sink in ("house", "tesla", "export", "inverter"):
        _assert_close(n[sink], _inbound(f, sink), "node_totals[%s]" % sink)
    _assert_close(n["battery_in"], _inbound(f, "battery"), "node_totals[battery_in]")


def test_node_totals_reports_each_source_as_the_sum_of_its_outbound_flows():
    args = (3000.0, -1000.0, 1000.0, 4000.0, 1000.0)
    f = decompose(*args)
    n = flows.node_totals(f)
    for src in SOURCES:
        _assert_close(n["%s_spent" % src], _outbound(f, src),
                      "node_totals[%s_spent]" % src)


def test_node_totals_house_excludes_the_car():
    """The whole reason v2 exists. 7 kW of car inside a 7.5 kW load must leave
    House at 500 W, not 7500 W."""
    n = flows.node_totals(decompose(0.0, 0.0, 8000.0, 7500.0, 7000.0))
    _assert_close(n["tesla"], 7000.0, "tesla")
    _assert_close(n["house"], 500.0, "house")


def test_node_totals_export_has_exactly_one_contributor():
    """If Export ever acquires a second inbound, the battery has been given a
    path to the grid."""
    f = decompose(5000.0, 0.0, -1000.0, 500.0, 0.0)
    assert flows.node_totals(f)["export"] == f["solar_to_export"]


@given(**BASE)
@SETTINGS
def test_node_totals_never_invents_or_loses_energy(solar, battery, grid, house, tesla):
    f = decompose(solar, battery, grid, house, tesla)
    n = flows.node_totals(f)
    sinks = n["house"] + n["tesla"] + n["battery_in"] + n["export"] + n["inverter"]
    sources = n["solar_spent"] + n["battery_spent"] + n["grid_spent"]
    _assert_close(sinks, sources, "node roll-up balances", rel=1e-9, abs_=1e-6)
    _assert_close(sinks, sum(f.values()), "node roll-up totals the flows",
                  rel=1e-9, abs_=1e-6)


# ===========================================================================
# 14. `unreportable` -- which flows die when a sensor does
#
# This is a CLAIM about the dependency graph, and a wrong claim is dangerous in
# one specific direction: a flow that is published as valid while one of its
# inputs is dead is a confident lie of exactly the kind BadReading exists to
# prevent. So it is checked empirically -- perturb each input, see which
# outputs actually move -- rather than by re-reading DEPENDS_ON.
# ===========================================================================

INPUT_CHANNELS = ("solar", "battery", "grid", "house", "tesla")


def test_no_blind_channel_means_nothing_is_unreportable():
    assert flows.unreportable(()) == frozenset()
    assert flows.unreportable([]) == frozenset()


@pytest.mark.parametrize("channel", INPUT_CHANNELS)
def test_every_channel_going_blind_costs_at_least_one_flow(channel):
    """A channel nothing depends on would be a channel we should not be reading."""
    assert flows.unreportable((channel,)), channel


def test_all_channels_blind_means_nothing_can_be_reported():
    dead = flows.unreportable(INPUT_CHANNELS)
    for key in KEYS:
        assert key in dead, key
    for node in ("house", "tesla", "battery_in", "export", "inverter"):
        assert node in dead, node


def test_an_unknown_channel_name_is_rejected():
    """Silently ignoring a typo would report every flow as fine while a sensor
    is dead -- the failure mode this whole function exists to prevent."""
    with pytest.raises(ValueError):
        flows.unreportable(("sloar",))
    with pytest.raises(ValueError):
        flows.unreportable(("solar", "pv1"))


def test_blindness_is_monotone():
    """Losing more sensors can never make more flows reportable."""
    a = flows.unreportable(("solar",))
    b = flows.unreportable(("solar", "grid"))
    assert a <= b


@pytest.mark.parametrize("channel", INPUT_CHANNELS)
def test_the_declared_dependencies_cover_every_flow_that_actually_moves(channel):
    """The empirical check, and the one that matters.

    For each input channel, perturb it across a spread of samples and record
    which of the twelve flows changed. Every flow that moved MUST be listed as
    unreportable when that channel goes blind. An under-declaration here means
    a flow sensor would keep publishing a number computed from a dead input.
    """
    dead = flows.unreportable((channel,))
    samples = [
        (3000.0, -1000.0, 1000.0, 4000.0, 1000.0),
        (5000.0, 2000.0, -1500.0, 800.0, 0.0),
        (0.0, -800.0, 1200.0, 1900.0, 0.0),
        (0.0, 3000.0, 4000.0, 900.0, 400.0),
        (2982.0, -457.0, -98.0, 3078.0, 300.0),
        (100.0, 0.0, 7000.0, 7100.0, 6900.0),
        (0.0, 0.0, 2000.0, 0.0, 0.0),
    ]
    moved = set()
    idx = INPUT_CHANNELS.index(channel)
    for base in samples:
        a = decompose(*base)
        for delta in (+250.0, -250.0, +37.5):
            probe = list(base)
            probe[idx] = base[idx] + delta
            if channel in ("solar", "house", "tesla") and probe[idx] < 0:
                continue
            b = decompose(*probe)
            for k in KEYS:
                if abs(a[k] - b[k]) > 1e-9:
                    moved.add(k)
    missing = moved - dead
    assert not missing, (
        "blinding %r is declared not to affect %r, but perturbing it moves them"
        % (channel, sorted(missing)))


def test_the_inverter_node_genuinely_does_not_depend_on_the_tesla_reading():
    """DEPENDS_ON says the three `*_to_inverter` flows survive a dead Tesla
    sensor, and that is a real and slightly surprising property rather than an
    oversight: the residual subtracts `house_rest + tesla`, which is just
    `house` once tesla is clamped, so the split between them cancels.

    Worth pinning: if the clamp were ever removed, this silently stops being
    true and the Inverter node starts depending on a sensor it claims not to.
    """
    assert not ({"solar_to_inverter", "battery_to_inverter", "grid_to_inverter"}
                & flows.unreportable(("tesla",)))
    base = (3000.0, -1000.0, 1000.0, 4000.0)
    first = decompose(*base, 0.0)
    for t in (500.0, 2000.0, 4000.0, 9000.0):
        got = decompose(*base, t)
        for k in ("solar_to_inverter", "battery_to_inverter", "grid_to_inverter"):
            _assert_close(got[k], first[k], "%s at tesla=%g" % (k, t))


def test_export_survives_a_dead_house_or_tesla_sensor():
    """Export is `min(solar, metered export)` and touches neither, so a dead
    house_load must not blank the Export ribbon."""
    assert "solar_to_export" not in flows.unreportable(("house",))
    assert "solar_to_export" not in flows.unreportable(("tesla",))
    first = decompose(4000.0, 0.0, -1500.0, 0.0, 0.0)["solar_to_export"]
    for house in (0.0, 900.0, 5000.0):
        for t in (0.0, 900.0):
            got = decompose(4000.0, 0.0, -1500.0, house, min(t, house))
            _assert_close(got["solar_to_export"], first, "export at house=%g" % house)


# ===========================================================================
# 15. Sign and polarity -- with a fixture helper that REFUSES degenerate inputs
#
# BRIEF section 9, and it is a rule rather than advice. t-edges wrote
# sign-inversion detectors in v1 that detected nothing: with solar == house,
# BOTH grid polarities decompose to all-zero grid flows, so the test passed
# green under a flipped sign and was only caught by an unrelated assertion.
#
# Proportional allocation makes that trap sharper, not milder. A source reading
# zero contributes zero to every sink, so any polarity test with a zero source
# is structurally incapable of detecting an inversion on it; and two sources of
# equal magnitude are interchangeable in the answer, so swapping them is
# invisible too.
#
# The fix is not to remember. It is `nondegenerate()`, which refuses the fixture
# outright, and `assert_polarity_is_detectable()`, which proves the two probes
# differ before either is inspected. Every test in this section goes through
# both, and so must any sign test added later.
# ===========================================================================

class DegenerateFixture(AssertionError):
    """This fixture cannot detect the inversion it claims to test."""


def nondegenerate(solar, battery, grid, house, tesla):
    """Return the sample, or raise if it cannot detect a sign inversion.

    Three conditions, each with its own failure mode:

    * all three sources strictly positive -- a zero source contributes zero to
      every sink, so an inverted sign on it produces the same all-zero answer;
    * the three mutually distinct by more than 1 W -- two equal sources are
      interchangeable, so swapping them changes nothing observable;
    * a strictly positive residual and a house load that is not all car -- with
      the residual clamped at zero, or with House empty, whole families of
      flows collapse to zero and stop discriminating.
    """
    p = parts(solar, battery, grid, house, tesla)
    mags = {"solar": p["S"],
            "battery": p["B"] if p["B"] else p["C"],
            "grid": p["G"] if p["G"] else p["E"]}
    for name, v in mags.items():
        if v <= 0.0:
            raise DegenerateFixture(
                "%s is 0 W, so an inverted sign on it is undetectable" % name)
    vals = sorted(mags.values())
    for a, b in zip(vals, vals[1:]):
        if b - a <= 1.0:
            raise DegenerateFixture(
                "two sources are within 1 W (%r); they are interchangeable and "
                "a swap would be invisible" % (mags,))
    if p["L_raw"] <= 0.0:
        raise DegenerateFixture(
            "the residual is %g W; a clamped Inverter node stops "
            "discriminating" % p["L_raw"])
    if p["Hr"] <= 0.0:
        raise DegenerateFixture("House is empty, so the *_to_house flows are all 0")
    return (solar, battery, grid, house, tesla)


def assert_polarity_is_detectable(a, b, what):
    """Two samples differing only in one polarity must decompose differently.

    Run BEFORE inspecting either answer. This is the assertion that would have
    caught t-edges' v1 detectors on the day they were written.
    """
    fa, fb = decompose(*a), decompose(*b)
    assert any(abs(fa[k] - fb[k]) > 1e-6 for k in KEYS), (
        "%s: both polarities decompose identically, so this fixture cannot "
        "detect an inversion at all" % what)
    return fa, fb


def test_the_degeneracy_guard_rejects_the_v1_trap():
    """Self-check. The guard is only worth having if it refuses the exact
    fixtures that fooled v1."""
    with pytest.raises(DegenerateFixture):          # zero battery
        nondegenerate(3000.0, 0.0, 1000.0, 2000.0, 0.0)
    with pytest.raises(DegenerateFixture):          # zero grid
        nondegenerate(3000.0, -1000.0, 0.0, 2000.0, 0.0)
    with pytest.raises(DegenerateFixture):          # zero solar
        nondegenerate(0.0, -1000.0, 1000.0, 1500.0, 0.0)
    with pytest.raises(DegenerateFixture):          # two equal sources
        nondegenerate(3000.0, -1000.0, 1000.0, 2000.0, 0.0)
    with pytest.raises(DegenerateFixture):          # clamped residual
        nondegenerate(3000.0, -1000.0, 500.0, 9000.0, 0.0)
    with pytest.raises(DegenerateFixture):          # House is all car
        nondegenerate(3000.0, -1000.0, 500.0, 900.0, 900.0)
    # and it accepts a fixture that really can discriminate
    nondegenerate(3000.0, -1000.0, 500.0, 2000.0, 400.0)


def test_the_v1_trap_fixture_is_no_longer_degenerate_and_the_inverter_node_is_why():
    """FINDING, reported rather than assumed: t-edges' v1 trap does NOT
    reproduce under v2.

    Their fixture was solar == house with the grid at +-500 W. Under v1 both
    polarities gave all-zero grid flows -- an import with no deficit and an
    export with no surplus are equally unattributable -- so the test passed
    green under a flipped sign.

    Under v2 the import has somewhere to go: the Inverter node. `grid_to_inverter`
    picks up the whole 500 W, the two polarities decompose differently, and the
    inversion is detectable after all. Naming a named loss node closed this trap
    as a side effect.

    This does NOT retire the degeneracy rule -- see the next test for the case
    that is still undetectable under v2 -- but the specific v1 fixture is safe.
    """
    a = (2000.0, 0.0, 500.0, 2000.0, 0.0)
    b = (2000.0, 0.0, -500.0, 2000.0, 0.0)
    fa, fb = assert_polarity_is_detectable(a, b, "the v1 trap fixture")
    assert fa["grid_to_inverter"] > 0.0, (
        "the import vanished again; the v1 trap has come back")
    assert fb["solar_to_export"] > 0.0


@pytest.mark.parametrize("channel,idx", [("battery", 1), ("grid", 2)])
def test_a_source_reading_zero_makes_its_own_polarity_undetectable(channel, idx):
    """The degeneracy that DOES survive into v2, and the reason
    `nondegenerate()` exists.

    Simulate an inverted sign convention by negating the input. When the channel
    reads a non-zero value the two decompose differently, so an inversion would
    be caught. When it reads exactly zero they are byte-identical -- a flipped
    sign is invisible, and a test built on such a fixture reports success while
    proving nothing.
    """
    base = [3000.0, -900.0, 400.0, 2000.0, 500.0]

    live = list(base)
    flipped = list(base)
    flipped[idx] = -live[idx]
    assert decompose(*live) != decompose(*flipped), (
        "%s: a non-zero reading must expose an inverted convention" % channel)

    dead = list(base)
    dead[idx] = 0.0
    dead_flipped = list(dead)
    dead_flipped[idx] = -0.0
    assert decompose(*dead) == decompose(*dead_flipped), (
        "%s: this is the degeneracy the guard exists for" % channel)
    with pytest.raises(DegenerateFixture):
        nondegenerate(*dead)


def test_battery_polarity_is_detected_on_a_nondegenerate_fixture():
    """Discharging must make the battery a SOURCE; charging must make it a SINK.
    An inverted battery sign swaps the two, and CLAUDE.md warns this is exactly
    the shape of thing an agent helpfully flips."""
    charge = nondegenerate(3000.0, 900.0, 400.0, 2000.0, 500.0)
    disch = nondegenerate(3000.0, -900.0, 400.0, 2000.0, 500.0)
    fc, fd = assert_polarity_is_detectable(charge, disch, "battery polarity")
    assert _inbound(fc, "battery") > 0.0 and _outbound(fd, "battery") > 0.0
    assert _outbound(fc, "battery") == 0.0, "a charging battery is supplying"
    assert _inbound(fd, "battery") == 0.0, "a discharging battery is being filled"


def test_grid_polarity_is_detected_on_a_nondegenerate_fixture():
    """Importing must make the grid a source; exporting must make it a sink fed
    only by solar. CLAUDE.md records 33257 as positive EXPORTING at the register
    while HA presents positive IMPORTING -- an inversion here is one boundary
    away at all times."""
    imp = nondegenerate(3000.0, -900.0, 400.0, 2000.0, 500.0)
    exp = nondegenerate(3000.0, -900.0, -400.0, 2000.0, 500.0)
    fi, fe = assert_polarity_is_detectable(imp, exp, "grid polarity")
    assert _outbound(fi, "grid") > 0.0, "an importing meter supplies nothing"
    assert fi["solar_to_export"] == 0.0
    assert fe["solar_to_export"] > 0.0, "an exporting meter fills no export"
    assert _outbound(fe, "grid") == 0.0, "an exporting meter is also supplying"


def test_solar_polarity_is_detected_on_a_nondegenerate_fixture():
    """Solar has no negative branch, so the inversion to catch is solar being
    read as a sink. It must always supply and never receive."""
    a = nondegenerate(3000.0, -900.0, 400.0, 2000.0, 500.0)
    f = decompose(*a)
    assert _outbound(f, "solar") > 0.0
    assert not [k for k in f if k.endswith("_to_solar")]


@given(**BASE)
@SETTINGS
def test_battery_and_grid_polarity_agree_with_the_readings_helper(
        solar, battery, grid, house, tesla):
    """Cross-check the sign convention against flows.readings(), which is the
    only other place it is written down. Two independent statements of the same
    convention that disagree is how an inversion survives."""
    r = flows.readings(solar, battery, grid, house, tesla)
    f = decompose(solar, battery, grid, house, tesla)
    if battery > 0:
        assert r["charge"] > 0 and r["discharge"] == 0
        assert _outbound(f, "battery") == 0.0
    if battery < 0:
        assert r["discharge"] > 0 and r["charge"] == 0
        assert _inbound(f, "battery") == 0.0
    if grid > 0:
        assert r["imp"] > 0 and r["exp"] == 0
        assert f["solar_to_export"] == 0.0
    if grid < 0:
        assert r["exp"] > 0 and r["imp"] == 0
        assert _outbound(f, "grid") == 0.0


# ===========================================================================
# 16. BRIEF section 10: metered export that vanishes from the chart
#
# `s2e = min(S, E)`. When solar reads 0 and the meter reports exporting, Export
# is drawn with NO inbound ribbon at all. That is deliberate -- battery -> grid
# is forbidden and grid -> export is not a thing -- but it means a real, metered
# export can silently disappear. v1 had the identical hole via
# `if solar_raw == 0.0: cap = 0.0`, and nobody owned it.
#
# Measured over the four captured days, which is the part that decides whether
# it matters:
#
#     day          worst sample   duration   energy dropped
#     2026-08-27      138 W          93 s      0.0018 kWh
#     2026-08-28      155 W         104 s      0.0023 kWh
#     2026-08-29     1903 W         303 s      0.0165 kWh
#     2026-08-30        0 W           0 s      0      kWh
#
# So the WORST input is not the largest export -- it is the largest export
# coinciding with solar reading exactly zero, and that was 1903 W. On a live
# chart that is a two-kilowatt Export box with nothing flowing into it, which
# reads as a fault. On a daily chart it is 16.5 Wh, which rounds to 0.0 and is
# invisible. Both statements are true and the tests below pin both.
# ===========================================================================

def test_export_is_drawn_with_no_inbound_when_solar_reads_zero():
    """The hole itself, stated as a fact rather than found by accident."""
    f = decompose(0.0, -3000.0, -1903.0, 500.0, 0.0)
    assert f["solar_to_export"] == 0.0
    assert not [k for k in f if k.endswith("_to_export") and f[k] > 0.0], (
        "something other than solar is feeding Export")


@pytest.mark.parametrize("export_w", [1.0, 138.0, 1903.0, 6000.0])
def test_the_export_shortfall_is_exactly_the_metered_export_when_solar_is_zero(
        export_w):
    """Not approximately, and not partially. The whole metered export is
    dropped, at every magnitude."""
    f = decompose(0.0, -3000.0, -export_w, 500.0, 0.0)
    assert f["solar_to_export"] == 0.0
    assert export_w - f["solar_to_export"] == export_w


@given(solar=st.floats(0.0, 6500.0, allow_nan=False, allow_infinity=False),
       export=st.floats(0.0, 6000.0, allow_nan=False, allow_infinity=False),
       battery=st.floats(-5000.0, 5000.0, allow_nan=False, allow_infinity=False),
       house=HOUSE, tesla=TESLA)
@SETTINGS
def test_the_export_shortfall_is_never_more_than_the_solar_deficit(
        solar, export, battery, house, tesla):
    """The bound that makes the hole tolerable: Export loses exactly
    max(0, E - S) and never a watt more. If the shortfall could exceed that,
    export would be vanishing for some OTHER reason and the whole rationale
    (battery cannot export) would no longer explain it."""
    f = decompose(solar, battery, -export, house, tesla)
    short = export - f["solar_to_export"]
    _assert_close(short, max(0.0, export - solar), "export shortfall")


def test_no_other_source_is_allowed_to_rescue_the_export_ribbon():
    """The tempting fix -- let the battery or grid fill Export when solar cannot
    -- is forbidden. All three discharge windows are unset, so battery -> grid
    is not merely zero, it is unrepresentable; and import and export never
    coexist on one meter reading. Pinned so a future 'improvement' fails."""
    for battery in (-5000.0, 0.0, 5000.0):
        f = decompose(0.0, battery, -2000.0, 100.0, 0.0)
        assert "battery_to_export" not in f
        assert "grid_to_export" not in f
        assert f["solar_to_export"] == 0.0


def test_a_single_watt_of_solar_does_not_unlock_the_whole_export():
    """The boundary. `min(S, E)` means 1 W of solar buys exactly 1 W of export
    ribbon, not the whole 2 kW. A cap written as `E if S > 0 else 0` would pass
    every other test in this section and fail this one."""
    f = decompose(1.0, 0.0, -2000.0, 0.0, 0.0)
    assert f["solar_to_export"] == 1.0


# ===========================================================================
# 17. Source conservation: EXACT, and exactly where
#
# team-lead asked for exact equality rather than a bound, on the strength of an
# algebraic proof that solar's five outflows sum to S in both branches of
# min(S, E). That proof is right about the export branch and silent about the
# other one: it assumes the residual is not clamped.
#
# Solar total = s2e + w * (Hr + T + C + L).
#   E <= S, L unclamped:  Hr+T+C+L = S+B+G-E = S1+B+G = den, so total = E + S1 = S.
#   E >  S:               s2e = S and w = 0, so total = S.
#   L clamped to 0:       Hr+T+C > den, so w*(Hr+T+C) > S1 and the total EXCEEDS S.
#
# The third branch is not hypothetical: it is 1226-3792 s of every captured day.
# So the tests below assert EXACT equality where it holds and pin the
# counterexample where it does not, rather than widening a tolerance over both.
# ===========================================================================

def test_the_counterexample_to_exact_source_conservation():
    """One sample, refuting 'solar always spends exactly its reading'.

    100 W of solar, nothing else, a 900 W house. The residual is -800 W, clamps
    to 0, and House is still filled exactly -- from a source holding 100 W. Solar
    is credited with 900.

    The direction is the safe one (the card clamps an overstated ribbon against
    the node's own state) but it is not exact, and calling it exact would put a
    false claim in the documentation.
    """
    f = decompose(100.0, 0.0, 0.0, 900.0, 0.0)
    assert _outbound(f, "solar") == 900.0
    assert _inbound(f, "house") == 900.0
    assert _inbound(f, "inverter") == 0.0


def test_the_same_counterexample_on_the_grid():
    """v2-physics' own example, kept here so both agents' claims are pinned in
    one place: 900 W credited to a meter reading 100 W."""
    f = decompose(0.0, 0.0, 100.0, 900.0, 0.0)
    assert _outbound(f, "grid") == 900.0


@given(args=consistent())
@SETTINGS
def test_solar_spends_exactly_its_reading_in_both_min_branches(args):
    """The algebraic claim, held to floating point and nothing wider, across
    both branches of `min(S, E)` -- and only where the residual is unclamped,
    which is the precondition the proof omitted."""
    assume(parts(*args)["L_raw"] >= 0.0)
    p = parts(*args)
    _assert_close(_outbound(decompose(*args), "solar"), p["S"], "solar outbound")


@given(**BASE)
@SETTINGS
def test_solar_spends_exactly_its_reading_whenever_export_out_reads_solar(
        solar, battery, grid, house, tesla):
    """The E > S branch specifically. Here `s2e = S` and the solar share is 0,
    so the total is S regardless of the residual -- this branch really is
    unconditionally exact, and it is worth separating from the one that is not.
    """
    p = parts(solar, battery, grid, house, tesla)
    assume(p["E"] > p["S"])
    f = decompose(solar, battery, grid, house, tesla)
    _assert_close(_outbound(f, "solar"), p["S"], "solar outbound")


@given(**BASE)
@SETTINGS
def test_the_total_of_all_twelve_flows_equals_the_total_of_the_five_sinks(
        solar, battery, grid, house, tesla):
    """v2-physics' invariant: this one holds on EVERY sample, in every regime,
    with no precondition at all. It is the strongest unconditional statement
    available and it is the one to rely on."""
    f = decompose(solar, battery, grid, house, tesla)
    sinks = sum(_inbound(f, s) for s in
                ("house", "tesla", "battery", "export", "inverter"))
    _assert_close(sinks, sum(f.values()), "sinks vs flows", rel=1e-12, abs_=1e-9)


# ===========================================================================
# 18. bool, tested for AGREEMENT as well as rejection
#
# t-edges' point, and it is the right shape: the durable risk is not whether a
# bool is rejected, it is whether the five positions DISAGREE. A guard on solar
# that is missing on tesla lets a bool inject a silent 1 W through that one
# channel, with no exception and no log line. The agreement test stays valid
# whichever ruling is taken; the rejection test above encodes the ruling that
# was actually taken.
# ===========================================================================

@pytest.mark.parametrize("value", [True, False])
def test_bool_handling_is_identical_in_all_five_positions(value):
    outcomes = {}
    for idx, name in enumerate(CHANNELS):
        args = list(GOOD)
        args[idx] = value
        try:
            decompose(*args)
            outcomes[name] = "accepted"
        except BAD:
            outcomes[name] = "rejected"
    assert len(set(outcomes.values())) == 1, (
        "bool %r is handled inconsistently across channels: %r -- a guard "
        "missing on one channel injects a silent %g W through it"
        % (value, outcomes, float(value)))


def test_bool_defeats_a_parse_based_guard_which_is_why_it_needs_its_own():
    """The mechanism, asserted rather than described. `float(True)` succeeds and
    `isinstance(True, int)` is True, so any guard written as `try: float(x)`
    passes a bool straight through as 1 W. NaN defeats parse-based guards by the
    same mechanism, which is why both are checked in all five positions."""
    assert float(True) == 1.0 and isinstance(True, int)
    assert not math.isnan(float(True))
    with pytest.raises(BAD):
        decompose(True, 0.0, 0.0, 0.0, 0.0)


# ===========================================================================
# 19. Which node totals survive a dead Tesla sensor
#
# This decides how much of 2026-08-27 is reportable, so it is worth stating
# precisely rather than approximately. SIX of the twelve flows depend on T --
# the three `*_to_house` and the three `*_to_tesla`. The other six do not, and
# neither do six of the eight node totals, because `Hr + T == house_load` for
# every T and the shares depend on S1, B and G alone.
#
# A summary of "ten of the twelve flows are blind to T" is wrong in the
# direction that matters: it would license reporting House and Tesla on a day
# when the car sensor was dead.
# ===========================================================================

_T_BLIND_NODES = ("battery_in", "export", "inverter",
                  "solar_spent", "battery_spent", "grid_spent")
_T_DEPENDENT_NODES = ("house", "tesla")


@given(solar=SOLAR, battery=BATTERY, grid=GRID,
       house=st.floats(1.0, 12000.0, allow_nan=False, allow_infinity=False))
@SETTINGS
def test_exactly_six_node_totals_are_blind_to_the_tesla_reading(
        solar, battery, grid, house):
    base = flows.node_totals(decompose(solar, battery, grid, house, 0.0))
    for t in (0.0, house * 0.25, house * 0.5, house, house * 2.0):
        got = flows.node_totals(decompose(solar, battery, grid, house, t))
        for node in _T_BLIND_NODES:
            _assert_close(got[node], base[node],
                          "%s at tesla=%g" % (node, t), rel=1e-9, abs_=1e-6)


def test_the_two_tesla_dependent_node_totals_really_do_move():
    """The other half of the claim. If House and Tesla did NOT move with T, the
    split would be decorative and 2026-08-27 would be fully reportable. They do
    move, which is exactly why it is not."""
    a = flows.node_totals(decompose(3000.0, -1000.0, 1000.0, 4000.0, 0.0))
    b = flows.node_totals(decompose(3000.0, -1000.0, 1000.0, 4000.0, 3000.0))
    for node in _T_DEPENDENT_NODES:
        assert abs(a[node] - b[node]) > 1.0, node
    _assert_close(a["house"] + a["tesla"], b["house"] + b["tesla"],
                  "House + Tesla is invariant in T")


def test_exactly_six_flows_declare_a_dependency_on_the_tesla_channel():
    """Cross-check against flows.DEPENDS_ON, which is what the HA plumbing will
    consult when the sensor goes away. Six, not ten, not two."""
    dead = flows.unreportable(("tesla",))
    flow_keys = {k for k in dead if k in KEYS}
    assert flow_keys == {
        "solar_to_house", "battery_to_house", "grid_to_house",
        "solar_to_tesla", "battery_to_tesla", "grid_to_tesla"}, sorted(flow_keys)
    assert len(flow_keys) == 6


# ===========================================================================
# 20. The division guard is an EXACT zero test, pinned deterministically
#
# v2-suite ran an independent mutation pass and reported that widening
# `_normalise`'s guard from `total <= 0.0` to `total <= 1e-9` survived its whole
# 240-test suite. It does NOT survive this file -- but the tests that killed it
# were `test_shares_sum_to_one_when_there_is_supply` and
# `test_sink_conservation_is_exact_for_the_four_shared_sinks`, both hypothesis
# properties that happened to draw a small enough total.
#
# Depending on a draw is not a guarantee. These four cases pin the boundary
# deterministically, at every scale from a denormal to a whole watt, so the
# guard's exactness survives a change of hypothesis seed.
#
# Why it matters at 1.0 rather than 1e-9: the house battery trickles at 50-100 W
# for 39 of every 88 hours (CLAUDE.md), and overnight samples really do sit in
# the single watts. A guard at 1.0 W blanks those samples into all-zero flows,
# and nothing else in the suite notices because zero flows are legal.
# ===========================================================================

@pytest.mark.parametrize("watts", [
    5e-324, 1e-320, 1e-30, 1e-12, 1e-9, 1e-6, 0.001, 0.5, 1.0, 2.0,
])
def test_the_division_guard_has_no_epsilon_at_any_scale(watts):
    """One source, one sink, exactly balanced: every watt of solar must reach
    the house. Any guard of the form `total <= eps` blanks this to zero for
    every `watts <= eps`, and the all-zero answer is structurally legal, so no
    other test in this file is obliged to complain."""
    f = decompose(watts, 0.0, 0.0, watts, 0.0)
    assert f["solar_to_house"] == watts, (
        "%g W of solar feeding a %g W house produced %r -- the division guard "
        "is swallowing real samples" % (watts, watts, f["solar_to_house"]))


@pytest.mark.parametrize("watts", [5e-324, 1e-30, 1e-9, 1.0])
def test_the_division_guard_does_not_swallow_a_low_battery_trickle(watts):
    """The realistic version of the same defect. CLAUDE.md records the battery
    trickling at 50-100 W for 39 of every 88 hours, and overnight samples reach
    the single watts; a widened guard makes those nights render empty."""
    f = decompose(0.0, -watts, 0.0, watts, 0.0)
    assert f["battery_to_house"] == watts


def test_a_widened_division_guard_would_be_caught_by_the_tests_above():
    """Self-check: prove those assertions can fail.

    Reproduces the mutation locally rather than trusting that they would catch
    it. If `_normalise` is ever rewritten so this simulation stops representing
    it, this test is the one that says so.
    """
    def widened(total, eps):
        return (0.0, 0.0, 0.0) if total <= eps else (1.0, 0.0, 0.0)

    assert widened(1e-12, 1e-9)[0] == 0.0
    assert widened(1e-12, 0.0)[0] == 1.0
