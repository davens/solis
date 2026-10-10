// Scenario overrides keyed by card-config key, deep-merged onto the snapshot:
// { time:'HH:MM', states:{ key:{state, attributes:{...}, minsAgo} } }. 'lights.N' is the Nth configured light, 'lights.*' all.
window.SCENARIOS = {
  normal: { time: '14:32', states: {} },
  live: { time: '13:05', states: { solar_power: { state: '4210' }, house_load: { state: '640' }, grid_power: { state: '-2950' }, battery_power: { state: '1450' } } },
  'live-low': { time: '10:20', states: { battery: { state: '58' }, ping_morning: { state: '2026-10-02 09:00:00' }, solar_power: { state: '420' }, house_load: { state: '1210' }, grid_power: { state: '380' }, battery_power: { state: '-430' } } },
  garage: { time: '14:32', states: { garage: { state: 'on', minsAgo: 20 } } },
  'kiln-evening': { time: '19:40', states: {
    kiln_firing: { state: 'on' }, kiln_warning: { state: 'on' }, battery: { state: '41' }, 'lights.2': { state: 'off' } } },
  'low-power': { time: '20:15', states: {
    battery: { state: '22' }, evening_gap: { state: '1.4' }, low_power: { state: 'on' },
    heating: { state: 'heat', attributes: { current_temperature: 19, temperature: 21, hvac_action: 'heating' } },
    aircon: { state: 'off' } } },
  'car-cooling': { time: '14:32', states: {
    car_climate: { state: 'heat_cool', attributes: { current_temperature: 31, temperature: 21 } },
    car_battery: { state: '100' }, money_export: { state: '9.5' } } },
  night: { time: '23:48', states: {
    battery: { state: '35' }, car_climate: { state: 'unavailable' }, 'lights.*': { state: 'off' } } },
  // The kiln warning under its band with the live row showing, as it is whenever the inverter reports: the fullest left panel.
  'kiln-live': { time: '19:40', states: { kiln_firing: { state: 'on' }, kiln_warning: { state: 'on' }, battery: { state: '41' },
    solar_power: { state: '0' }, house_load: { state: '2350' }, grid_power: { state: '1900' }, battery_power: { state: '-450' } } },
  'all-alerts': { time: '14:32', states: {
    garage: { state: 'on', minsAgo: 20 }, kiln_firing: { state: 'on' }, kiln_warning: { state: 'on' } } },
  'garage-car': { time: '12:20', states: { garage: { state: 'on', minsAgo: 4 }, battery: { state: '100' }, solar_power: { state: '2800' }, house_load: { state: '500' }, grid_power: { state: '-2200' }, battery_power: { state: '-650' },
    car_climate: { state: 'cool', attributes: { current_temperature: 21, temperature: 21 } },
    car_state: { state: 'online' }, car_plugged: { state: 'off' }, car_locked: { state: 'off' }, car_link: { state: 'on' }, car_charging: { state: 'Disconnected' }, car_power: { state: '0' }, car_battery: { state: '72' } } },
  'car-battery': { time: '13:40', octopus: { slots: [[-20, 40]], target: 80, by: '07:00:00' }, states: { battery: { state: '64' }, solar_power: { state: '1900' }, house_load: { state: '7900' }, grid_power: { state: '3800' }, battery_power: { state: '-2150' },
    car_state: { state: 'charging' }, car_plugged: { state: 'on' }, car_locked: { state: 'on' }, car_link: { state: 'on' }, car_charging: { state: 'Charging' }, car_power: { state: '7.1' }, car_energy_added: { state: '3.2' }, car_time_to_full: { state: '2.5' }, car_battery: { state: '58' } } },
  // The dishwasher in the battery's note line. atMins sets a timestamp state that many minutes from the frozen time.
  'dish-running': { time: '18:40', states: { dishwasher: { state: 'run' }, dishwasher_programme: { state: 'dishcare_dishwasher_program_eco_50' }, dishwasher_finish: { atMins: 80 }, dishwasher_door: { state: 'closed' },
    solar_power: { state: '150' }, house_load: { state: '1900' }, grid_power: { state: '0' }, battery_power: { state: '-1750' } } },
  // Home Connect missed the start event: state still 'ready', no finish time yet.
  'dish-missed': { time: '15:20', states: { dishwasher: { state: 'ready', minsAgo: 35 }, dishwasher_programme: { state: 'dishcare_dishwasher_program_eco_50', minsAgo: 1 }, dishwasher_finish: { state: 'unavailable' }, dishwasher_door: { state: 'closed' } } },
  'dish-delayed': { time: '21:10', states: { dishwasher: { state: 'delayedstart' }, dishwasher_programme: { state: 'dishcare_dishwasher_program_auto_2' }, dishwasher_finish: { atMins: 600 }, dishwasher_door: { state: 'closed' } } },
  'dish-done': { time: '20:05', states: { dishwasher: { state: 'finished', minsAgo: 18 }, dishwasher_programme: { state: 'unknown' }, dishwasher_finish: { state: 'unavailable' }, dishwasher_door: { state: 'closed' } } },
  // Under the garage band, the tightest left column.
  'dish-garage': { time: '18:40', states: { garage: { state: 'on', minsAgo: 6 }, dishwasher: { state: 'run' }, dishwasher_programme: { state: 'dishcare_dishwasher_program_intensiv_70' }, dishwasher_finish: { atMins: 135 }, dishwasher_door: { state: 'closed' },
    solar_power: { state: '150' }, house_load: { state: '1900' }, grid_power: { state: '0' }, battery_power: { state: '-1750' } } },
  // Car states, as on the Overview car tile.
  'car-parked': { time: '14:20', states: { car_state: { state: 'online' }, car_plugged: { state: 'off' }, car_locked: { state: 'on' }, car_link: { state: 'on' }, car_charging: { state: 'Disconnected' }, car_power: { state: '0' }, car_battery: { state: '72' } } },
  'car-evening': { time: '21:40', states: { car_state: { state: 'online' }, car_plugged: { state: 'off' }, car_locked: { state: 'on' }, car_link: { state: 'on' }, car_charging: { state: 'Disconnected' }, car_power: { state: '0' }, car_battery: { state: '40' } } },
  'car-driving': { time: '08:10', states: { car_state: { state: 'driving' }, car_speed: { state: '55' }, car_plugged: { state: 'off' }, car_link: { state: 'on' }, car_charging: { state: 'Disconnected' }, car_power: { state: '0' }, car_battery: { state: '78' } } },
  'car-alerts': { time: '16:00', states: { car_state: { state: 'asleep' }, car_plugged: { state: 'off' }, car_locked: { state: 'off' }, car_link: { state: 'on' }, car_charging: { state: 'Disconnected' }, car_power: { state: '0' }, car_battery: { state: '64' }, 'car_alerts.2': { state: 'on' }, 'car_alerts.4': { state: 'on' } } },
  'car-waiting': { time: '21:40', octopus: { slots: [[180, 200], [260, 290]], target: 80, by: '07:00:00' }, states: { car_state: { state: 'asleep' }, car_plugged: { state: 'on' }, car_locked: { state: 'on' }, car_link: { state: 'on' }, car_charging: { state: 'Stopped' }, car_power: { state: '0' }, car_battery: { state: '40' } } },
  'car-charging': { time: '00:52', octopus: { slots: [[-12, 8], [68, 98]], target: 80, by: '07:00:00' }, states: { car_state: { state: 'charging' }, car_plugged: { state: 'on' }, car_locked: { state: 'on' }, car_link: { state: 'on' }, car_charging: { state: 'Charging' }, car_power: { state: '7.2' }, car_energy_added: { state: '8.4' }, car_time_to_full: { state: '3.17' }, car_battery: { state: '52' } } },
  'car-complete': { time: '06:30', octopus: { slots: [], target: 80, by: '07:00:00' }, states: { car_state: { state: 'asleep' }, car_plugged: { state: 'on' }, car_locked: { state: 'on' }, car_link: { state: 'on' }, car_charging: { state: 'Complete' }, car_power: { state: '0' }, car_energy_added: { state: '18.4' }, car_battery: { state: '80' } } },
  'car-nolink': { time: '12:00', states: { car_link: { state: 'off' }, car_battery: { state: '64', last_updated: '2026-10-02T10:12:00Z' } } },
};
// Each car state again under the garage band, where the car panel is shortest.
for (const n of Object.keys(window.SCENARIOS).filter(k => k.startsWith('car-'))) {
  const c = window.SCENARIOS[n];
  window.SCENARIOS[`band-${n}`] = { ...c, states: { ...c.states, garage: { state: 'on', minsAgo: 4 } } };
}
