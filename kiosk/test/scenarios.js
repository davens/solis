// Scenario overrides keyed by card-config key, deep-merged onto the snapshot:
// { time:'HH:MM', states:{ key:{state, attributes:{...}, minsAgo} } }. 'lights.N' is the Nth configured light, 'lights.*' all.
window.SCENARIOS = {
  normal: { time: '14:32', states: {} },
  garage: { time: '14:32', states: { garage: { state: 'on', minsAgo: 20 } } },
  'kiln-evening': { time: '19:40', states: {
    kiln_firing: { state: 'on' }, kiln_warning: { state: 'on' }, battery: { state: '41' }, 'lights.2': { state: 'off' } } },
  'low-power': { time: '20:15', states: {
    battery: { state: '22' }, evening_gap: { state: '1.4' },
    heating: { state: 'heat', attributes: { current_temperature: 19, temperature: 21, hvac_action: 'heating' } },
    aircon: { state: 'off' } } },
  'car-cooling': { time: '14:32', states: {
    car_climate: { state: 'heat_cool', attributes: { current_temperature: 31, temperature: 21 } },
    car_battery: { state: '100' }, money_export: { state: '9.5' } } },
  night: { time: '23:48', states: {
    battery: { state: '35' }, car_climate: { state: 'unavailable' }, 'lights.*': { state: 'off' } } },
  'all-alerts': { time: '14:32', states: {
    garage: { state: 'on', minsAgo: 20 }, kiln_firing: { state: 'on' }, kiln_warning: { state: 'on' } } },
};
