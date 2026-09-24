"""Regenerate flow_sensors_v2.yaml -- the fifteen flow power templates.

The Jinja preamble is identical in all fifteen sensors and only the final
expression differs, so the file is generated rather than hand-maintained:
test_jinja_parity.py asserts the fifteen copies are byte-identical, and a
hand-edit that touched one copy would fail that test rather than ship a quiet
disagreement between two ribbons.

Run:
    uv run --no-project python gen_flow_yaml_v2.py        # rewrites the YAML
    uv run --no-project python gen_flow_yaml_v2.py --check  # exit 1 if stale
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
YAML_PATH = os.path.join(HERE, "flow_sensors_v2.yaml")
HEADER_PATH = os.path.join(HERE, "_flow_yaml_header.txt")

# (entity slug, flows.py key, the expression the state template ends on).
#
# `grid` as a sink is EXPORT and `battery` as a sink is CHARGE: the entity ids
# name the physical thing the meter measures, flows.py names the graph node.
# The seam is deliberate and is pinned by test_jinja_parity.SLUG.
SENSORS = (
    ("solar_to_grid",       "solar_to_export",     "s2e"),
    ("solar_to_battery",    "solar_to_battery",    "C * w"),
    ("solar_to_house",      "solar_to_house",      "Hr * w"),
    ("solar_to_tesla",      "solar_to_tesla",      "T * w"),
    ("solar_to_aircon",     "solar_to_aircon",     "A * w"),
    ("solar_to_inverter",   "solar_to_inverter",   "L * w"),
    ("battery_to_house",    "battery_to_house",    "Hr * b"),
    ("battery_to_tesla",    "battery_to_tesla",    "T * b"),
    ("battery_to_aircon",   "battery_to_aircon",   "A * b"),
    ("battery_to_inverter", "battery_to_inverter", "L * b"),
    ("grid_to_house",       "grid_to_house",       "Hr * g"),
    ("grid_to_tesla",       "grid_to_tesla",       "T * g"),
    ("grid_to_aircon",      "grid_to_aircon",      "A * g"),
    ("grid_to_battery",     "grid_to_battery",     "C * g"),
    ("grid_to_inverter",    "grid_to_inverter",    "L * g"),
)

AVAILABILITY = """\
          {% set vs = [ 'sensor.solis_inverter_solar_power',
                        'sensor.solis_inverter_battery_power',
                        'sensor.solis_inverter_grid_power',
                        'sensor.solis_inverter_house_load',
                        'sensor.tesla_home_charging_power' ]
                      | map('states') | select('is_number') | map('float')
                      | map('abs') | list %}
          {{ vs | count == 5 and (vs | max) <= 100000 }}"""

# Air con is read with `| float(0)` and is NOT in the availability list; the
# header explains why at length. Everything else carries no default on purpose.
PREAMBLE = """\
          {% set S  = [ 0, states('sensor.solis_inverter_solar_power')   | float ] | max %}
          {% set BP =      states('sensor.solis_inverter_battery_power') | float %}
          {% set GP =      states('sensor.solis_inverter_grid_power')    | float %}
          {% set H  = [ 0, states('sensor.solis_inverter_house_load')    | float ] | max %}
          {% set TR = [ 0, states('sensor.tesla_home_charging_power')    | float ] | max %}
          {% set AR = [ 0, states('sensor.aircon_power')              | float(0) ] | max %}
          {% set B  = [ 0, -BP ] | max %}
          {% set C  = [ 0,  BP ] | max %}
          {% set G  = [ 0,  GP ] | max %}
          {% set E  = [ 0, -GP ] | max %}
          {% set T  = [ TR, H ] | min %}
          {% set A  = [ AR, H - T ] | min %}
          {% set Hr = [ 0, H - T - A ] | max %}
          {% set L  = [ 0, (S + B + G) - (Hr + T + A + C + E) ] | max %}
          {% set s2e = [ S, E ] | min %}
          {% set S1 = S - s2e %}
          {% set tot = S1 + B + G %}
          {% set w = S1 / tot if tot > 0 else 0 %}
          {% set b = B  / tot if tot > 0 else 0 %}
          {% set g = G  / tot if tot > 0 else 0 %}"""


def _title(slug):
    return "Flow " + slug.replace("_", " ")


def render():
    with open(HEADER_PATH) as fh:
        out = [fh.read(), "template:\n  - sensor:\n"]
    for i, (slug, key, expr) in enumerate(SENSORS):
        avail = "&v2_flow_avail >\n" + AVAILABILITY if i == 0 else "*v2_flow_avail"
        out.append(
            "      # ------- %s  (flows.py key: %s) -------\n"
            '      - name: "%s power"\n'
            "        unique_id: flow_%s_power\n"
            "        unit_of_measurement: W\n"
            "        device_class: power\n"
            "        state_class: measurement\n"
            "        availability: %s\n"
            "        state: >\n"
            "%s\n"
            "          {{ (%s) | round(4) }}\n\n"
            % (slug, key, _title(slug), slug, avail, PREAMBLE, expr)
        )
    return "".join(out).rstrip("\n") + "\n"


def main(argv):
    text = render()
    if "--check" in argv:
        with open(YAML_PATH) as fh:
            current = fh.read()
        if current != text:
            sys.stderr.write("flow_sensors_v2.yaml is stale; re-run the generator\n")
            return 1
        return 0
    with open(YAML_PATH, "w") as fh:
        fh.write(text)
    sys.stdout.write("wrote %s (%d sensors)\n" % (YAML_PATH, len(SENSORS)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
