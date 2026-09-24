# The solar forecast model: array geometry, the five-model ensemble and the fit

*Split out of `CLAUDE.md` on 2026-09-13 to keep the always-loaded file small. This is the same text, except that site-specific values are placeholders whose real values are in the gitignored `CLAUDE.local.md`. The rules and traps that apply even when you are NOT reading this file stayed in `CLAUDE.md`.*

## Forecast model reference

`solar_forecast.py` answers whether tomorrow's sun refills the battery or the off-peak rate should. Its
public standalone interface -- verified against source 2026-08-27 -- is `today_kwh()`, `tomorrow_kwh()`,
`verdict()`, `tomorrow_weather()`, plus `day_kwh()`, `hourly_kw()`, and the `record()`/`calibrate()` pair
that use `solar_actuals.json`. The vendored HA copy is a separate implementation with private names
(`_day_kwh`, `_verdict`, `_tomorrow_weather`, `fetch_forecast`); **do not assume a function exists in
both.** Open-Meteo is free, keyless, and includes past days. The site is configuration, not a constant:
the standalone script reads `SITE_LATITUDE`/`SITE_LONGITUDE` from `.env`, and the vendored copy uses HA's
own home location (`hass.config.latitude`/`longitude`). The site both copies used to hard-code is in
`CLAUDE.local.md`.

### Surveyed array geometry

The geometry in `PLANES` is measured, not guessed; retain separate tilt as well as azimuth:

- Roof 1: 12 x 400 W, 207 deg SW compass / 21 deg pitch; survey 25.1 m2 and 990 kWh/kWp.
- Roof 2: 8 x 400 W, 117 deg SE compass / 24 deg pitch; survey 18 m2 and 920 kWh/kWp.
- Total: 20 panels, 8.0 kWp. Open-Meteo azimuth = compass - 180, hence +27 and -63.

The old guess, -15/-100 at one 25 deg pitch, put both planes too far east but a fitted `EFFECTIVE_KWP`
absorbed much of the error. Refitting six late-summer actuals moved RMSE only 1.66 -> 1.59; that is not
the reason to keep the survey. Keep it because it is measured and azimuth error grows away from
midsummer. Survey annual generation 4752 + 2944 = 7696 kWh is within 1% of 7743 at 33039/33040,
**suspected** to be last year's total but not confirmed.

### Five-model ensemble and fit

`MODELS` is `ecmwf_ifs025`, `gfs_seamless`, `icon_seamless`, `ukmo_seamless`, `meteofrance_seamless`.
Against seven actuals the mean scored **2.19 kWh RMSE**; the best individual scored 3.26 and worst 5.63.
Open-Meteo `best_match` resolves to UKMO here, which missed 2026-08-20 by 7 kWh (3.65 kWh/m2 versus
ECMWF 5.90), yet over all seven ECMWF was worst at 5.63 and UKMO mid-pack at 4.28. **There is no stable
winner: averaging is the point.** A complete-day all-zero model is missing/padded data, not darkness, and
must be dropped.

`EFFECTIVE_KWP` = 6.33 against 8.0 kWp gives a realistic 0.79 performance ratio. Earlier single-source
fits produced PR > 1.0, revealing bad weather input. `verdict()` returns low/borderline/high and refuses
a call within `UNCERTAINTY_KWH` = 2 kWh of `LOW_KWH` = 25 kWh.

Adding 2026-08-21, prediction 29.4 versus actual 26.0, was the first material over-prediction and moved
seven-day RMSE to about 2.1-2.2. This is soft: a cache refetch alone moved 2.07 -> 2.19 because
Open-Meteo revises the current day. Refitting 6.33 -> 6.24 saved only 0.06 kWh RMSE; bias moved +0.36 ->
-0.09. That is noise, so 6.33 stayed. **Model spread, not scale, is the remaining problem.**

forecast.solar was rejected because it badly low-balled the array: 3.5 kW peak and 26 kWh against an
observed roughly 6 kW peak and calibrated 39 kWh. A pvlib Perez transposition did no better than
Open-Meteo tilted irradiance. Do not retry either without new evidence.

**Never quote a kW peak from `hourly_kw()`.** Daily-energy fitting folds in part-load efficiency, diffuse
shoulder hours and low incidence angles that do not apply at noon. On 2026-08-22 the same curve gave
4.41 kW at 6.33 versus 5.58 at nameplate; five models ranged 5.55/4.86/4.66/4.51/3.50 kW and averaging
flattened the peak further. The owner correctly rejected "peaks around 13:00 at 4.4 kW" while the
inverter was near 6 kW. Daily kWh is right while instantaneous peak is wrong. `peakLine()` therefore
reports only the contiguous >80%-of-peak span, for example "strongest 11:00-15:00", with no number. A kW
claim needs a separate model and recorded peak actuals; none exist.

The 6 kW inverter ceiling costs little. The model estimated 0.7 kWh clipping on clear 2026-07-20, a
49 kWh actual day, but because it uses 6.33 effective kWp this **understates clipping** and is a floor,
not a measurement. Twelve southwest plus eight southeast panels broaden the curve. Do not propose a
bigger inverter on clipping grounds.

The daily `weather_code` is a worst-of-day aggregate and can say overcast beside high kWh. `_sky()` uses
codes for precipitation and mean cloud for clear/cloudy. `_daylight_sky()` restricts both to
sunrise-sunset; on 2026-08-21 a 03:00 code 51 but daylight max 3 changed "light drizzle" to "partly
cloudy". `tomorrow_weather()` also supplies today's sunrise/sunset so the UI can distinguish low
post-sunset PV from a fault: 16 W at 20:15 with sunset 20:08 is correct.

**`_weather_memo` must be keyed by target date as well as age.** With its 3600 s TTL, an age-only memo
created 23:55 still served the wrong "tomorrow" at 00:05 and for up to an hour. `_series()` is safe
because it caches a dict keyed by date.

Open-Meteo azimuth is verified as 0 south, -90 east, +90 west. Same-hour peaks across azimuths indicate
cloud, not a broken convention.

