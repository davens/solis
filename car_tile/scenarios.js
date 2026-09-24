// Scenario overrides on top of states_base.json (a live snapshot). Locations are FICTIONAL
// (central Cambridge), never the owner's real home.
const HOME = { lat: 52.2053, lon: 0.1218 };
const OCTO = 'octopus_energy_00000000_0000_0000_0000_000000000000_';
const iso = ms => new Date(ms).toISOString();
const helpers = {
  set(states, id, state, attrs = {}, agoMin = 0) {
    const prev = states[id] || { entity_id: id, attributes: {} };
    states[id] = { ...prev, entity_id: id, state: String(state), attributes: { ...prev.attributes, ...attrs },
                   last_changed: iso(Date.now() - agoMin * 60000), last_updated: iso(Date.now() - agoMin * 60000) };
  },
};
function common(states, h, o) {
  h.set(states, 'zone.home', 'zoning', { latitude: HOME.lat, longitude: HOME.lon, radius: 100, friendly_name: 'Home', icon: 'mdi:home' });
  h.set(states, 'device_tracker.tesla_location', o.where, { latitude: o.lat, longitude: o.lon, gps_accuracy: 0, source_type: 'gps', friendly_name: 'Tesla Location' }, o.whereAgo || 30);
  h.set(states, 'sensor.tesla_heading', o.heading, { unit_of_measurement: '°', friendly_name: 'Tesla Heading' });
  h.set(states, 'binary_sensor.garage_door', o.garage, { device_class: 'opening', friendly_name: 'Garage Door' }, o.garageAgo);
}
const slot = (startMin, endMin) => ({ start: iso(Date.now() + startMin * 60000), end: iso(Date.now() + endMin * 60000) });
const list = [
  { id: 'home_asleep', title: 'Home · plugged in, asleep · garage closed (the everyday state)',
    apply(s, h) {
      common(s, h, { where: 'home', lat: HOME.lat + 0.00012, lon: HOME.lon - 0.00018, heading: 119, garage: 'off', garageAgo: 300 });
      h.set(s, 'sensor.tesla_state', 'offline'); h.set(s, 'binary_sensor.tesla_plugged_in', 'on');
      h.set(s, 'sensor.tesla_charging_state', 'NoPower'); h.set(s, 'sensor.tesla_battery', 57); h.set(s, 'sensor.tesla_range', 242);
      h.set(s, 'binary_sensor.' + OCTO + 'intelligent_dispatching', 'off', { planned_dispatches: [slot(320, 680)] });
    } },
  { id: 'away_garage_open', title: 'Away, parked 8 km off · garage left open 40 min',
    apply(s, h) {
      common(s, h, { where: 'not_home', lat: 52.2610, lon: 0.1930, heading: 45, garage: 'on', garageAgo: 40 });
      h.set(s, 'sensor.tesla_state', 'online'); h.set(s, 'binary_sensor.tesla_plugged_in', 'off');
      h.set(s, 'sensor.tesla_charging_state', 'Disconnected'); h.set(s, 'sensor.tesla_battery', 64); h.set(s, 'sensor.tesla_range', 271);
      h.set(s, 'binary_sensor.tesla_locked', 'on');
    } },
  { id: 'home_charging_alerts', title: 'Home, charging · garage open 12 min · sentry + boot open (busiest case)',
    apply(s, h) {
      common(s, h, { where: 'home', lat: HOME.lat + 0.00012, lon: HOME.lon - 0.00018, heading: 119, garage: 'on', garageAgo: 12 });
      h.set(s, 'sensor.tesla_state', 'charging'); h.set(s, 'binary_sensor.tesla_plugged_in', 'on');
      h.set(s, 'sensor.tesla_charging_state', 'Charging'); h.set(s, 'sensor.tesla_charger_power', 7.2);
      h.set(s, 'sensor.tesla_time_to_full_charge', 1.4); h.set(s, 'sensor.tesla_charge_energy_added', 12.3);
      h.set(s, 'sensor.tesla_battery', 48); h.set(s, 'sensor.tesla_range', 205);
      h.set(s, 'binary_sensor.tesla_sentry', 'on'); h.set(s, 'binary_sensor.tesla_trunk_open', 'on');
      h.set(s, 'binary_sensor.' + OCTO + 'intelligent_dispatching', 'on', { planned_dispatches: [slot(-20, 70)] });
    } },
  { id: 'driving', title: 'Driving, 1.6 km from home · garage closed',
    apply(s, h) {
      common(s, h, { where: 'not_home', lat: 52.2150, lon: 0.1400, heading: 280, garage: 'off', garageAgo: 25, whereAgo: 0 });
      h.set(s, 'sensor.tesla_state', 'driving'); h.set(s, 'sensor.tesla_speed', 48); h.set(s, 'binary_sensor.tesla_plugged_in', 'off');
      h.set(s, 'sensor.tesla_charging_state', 'Disconnected'); h.set(s, 'sensor.tesla_battery', 71); h.set(s, 'sensor.tesla_range', 300);
    } },
];
module.exports = { list, helpers, HOME };
