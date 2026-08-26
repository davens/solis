"""Browser dashboard for the read-only Solis API.

Pure front end: one route serving the PAGE below; every number on it comes
from solis_api's /api/state, which the browser polls. Home Assistant does not
use this - it reads /api/state directly (see homeassistant.md).

Run it via settings_dash.py, which mounts this blueprint on the API app.
"""
import os

from flask import Blueprint, render_template_string

import solis_api

dash = Blueprint("dash", __name__)


@dash.route("/")
def index():
    return render_template_string(
        PAGE, poll=solis_api.POLL_SECONDS, raw_from=solis_api.TELEMETRY_BASE,
        raw_to=solis_api.TELEMETRY_BASE + solis_api.TELEMETRY_COUNT - 1,
        car_enabled=bool(os.environ.get("OCTOPUS_API_KEY")))


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solis Power Management</title>
<style>
  :root {
    --bg: #0a0d11; --panel: #131a22; --panel-hi: #18212b;
    --line: #253039; --line-soft: #1a232c;
    --text: #e9f1f8; --dim: #9aadbd;
    --lime: #c3f53c; --green: #2fe07a; --cyan: #2ad4ee;
    --amber: #ffb224; --violet: #b48bff; --blue: #4da3ff; --red: #ff5a52;
    --accent: var(--green);
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, "Roboto Mono", monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; color: var(--text);
    background:
      radial-gradient(900px 520px at 10% -12%, rgba(47,224,122,.10), transparent 62%),
      radial-gradient(760px 480px at 94% -6%, rgba(180,139,255,.10), transparent 62%),
      repeating-linear-gradient(0deg, rgba(255,255,255,.016) 0 1px, transparent 1px 46px),
      repeating-linear-gradient(90deg, rgba(255,255,255,.016) 0 1px, transparent 1px 46px),
      var(--bg);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }
  /* 1300 not 1120: six 196px tiles + gaps need 1236px, and the Car tile
     makes the top row six. */
  .wrap { max-width: 1300px; margin: 0 auto; padding: 30px 20px 64px; }
  header {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding-bottom: 16px; margin-bottom: 24px; border-bottom: 1px solid var(--line);
  }
  h1 { font-size: 17px; font-weight: 800; margin: 0; text-transform: uppercase; letter-spacing: .18em; }
  h1 .hi { color: var(--lime); margin-left: .45em; text-shadow: 0 0 18px rgba(195,245,60,.4); }
  .sub { color: var(--dim); font-size: 11px; font-family: var(--mono); letter-spacing: .05em; margin-left: auto; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--dim); margin-right: 7px; vertical-align: 1px; }
  .dot.live { background: var(--green); animation: pulse 2.4s infinite; }
  .dot.bad { background: var(--red); box-shadow: 0 0 10px var(--red); }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(47,224,122,.55); }
    70% { box-shadow: 0 0 0 8px rgba(47,224,122,0); }
    100% { box-shadow: 0 0 0 0 rgba(47,224,122,0); }
  }
  /* Stale data must not read as live data: dim the tiles once the cached
     sweep stops advancing, rather than only changing the status dot. */
  body.stale .grid { opacity: .4; filter: saturate(.35); transition: opacity .3s; }
  .grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(196px, 1fr)); margin-bottom: 26px; }
  .card {
    position: relative; overflow: hidden; border-radius: 4px; padding: 15px 16px 17px 19px;
    background: linear-gradient(180deg, var(--panel-hi), var(--panel)); border: 1px solid var(--line);
    display: flex; flex-direction: column;
  }
  /* The tiles carry different amounts of sub-detail - one note line, two, or a
     bar and a note - so with everything packed under the headline number the
     grey text finished at five different heights across the row. Absorbing the
     slack directly under the value instead drops every tile's sub-detail onto
     a common bottom edge; the headline numbers still line up because .label
     and .value are a fixed height above them. */
  .card > .value { margin-bottom: auto; }
  .card::before {
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
    background: var(--ac, var(--dim)); box-shadow: 0 0 16px var(--ac, transparent);
  }
  .card::after {
    content: ""; position: absolute; right: 9px; top: 9px; width: 9px; height: 9px;
    border-top: 1px solid var(--ac, var(--dim)); border-right: 1px solid var(--ac, var(--dim)); opacity: .45;
  }
  .card.pv { --ac: var(--lime); }
  .card.batt { --ac: var(--green); }
  .card.house { --ac: var(--cyan); }
  .card.day { --ac: var(--amber); }
  .card.mains { --ac: var(--violet); }
  .card.car { --ac: var(--blue); }
  /* Nothing is wrong at 20:03 - the sun has set. Say so instead of showing a
     bright 18 W that reads like a fault. */
  .card.night .value { color: var(--dim); text-shadow: none; }
  .card.night::before { box-shadow: none; opacity: .4; }
  .label {
    color: var(--dim); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .14em;
    margin-bottom: 11px; display: flex; align-items: center; gap: 7px;
  }
  .ico { width: 14px; height: 14px; flex: 0 0 auto; fill: none; stroke: currentColor;
         stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
  .card .label .ico { color: var(--ac, var(--dim)); }
  .value {
    font-family: var(--mono); font-size: 29px; font-weight: 600; line-height: 1.15;
    font-variant-numeric: tabular-nums; letter-spacing: -.02em;
  }
  .card .value { color: var(--ac, var(--text)); text-shadow: 0 0 20px color-mix(in srgb, currentColor 32%, transparent); }
  .value .unit { font-size: 12px; color: var(--dim); font-weight: 500; margin-left: 5px; letter-spacing: .08em; text-shadow: none; }
  /* Voltage rides the value row rather than owning a line of its own - the
     grid tile was one line taller than every other card and set the row
     height. Explicit dim color: the JS repaints #gvalue amber/green by flow
     direction, and the voltage must not follow it. */
  .value.vrow { display: flex; align-items: baseline; }
  /* The voltage lives in the card's top-right corner, on the label row -
     riding the value row (baseline or top-aligned, both tried) it read as
     part of the power figure. right:26px clears the corner bracket. */
  .value .vsub { position: absolute; top: 14px; right: 26px; font-size: 11px;
                 font-family: var(--mono); font-weight: 500; color: var(--dim);
                 text-shadow: none; }
  .note { color: var(--dim); font-size: 12px; margin-top: 6px; font-variant-numeric: tabular-nums; }
  /* Octopus car-charging status. It lives under the timeline because the
     23:30-05:30 window exists partly to stop the house battery feeding the
     car (see CLAUDE.md); a daytime dispatch is exactly that risk, hence amber. */
  .note.car { display: flex; align-items: center; gap: 7px; margin-top: 10px; }
  .note.car.charging { color: var(--text); }
  .note.car.daytime { color: var(--amber); }
  /* The sub-detail used to be sentences - "today 14.1 kWh / yesterday 21.8 kWh"
     strung along a line, wrapping to two or three lines depending on the
     figures. But nobody reads those as prose; they read them to compare two
     numbers, which wants a column, not a clause. Label left, figure right, one
     row each: nothing wraps, tile depth stops varying, and the figures line up
     into a single column that can be scanned down the whole row of tiles.
     The `·` also stops doing two jobs at once - it now only ever separates
     values inside one measurement, never whole clauses. */
  .mrow { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
  .mrow .k { color: var(--dim); white-space: nowrap; }
  .mrow .v { color: var(--text); font-family: var(--mono); white-space: nowrap; }
  .mrow .v.acc { color: var(--ac, var(--text)); }
  /* Car tile: the plug state rides the top-right corner, the same slot the
     grid tile uses for voltage. The dot colour is the state - green pulse
     charging, blue plugged in, grey unplugged, red no link - and the word
     disambiguates for anyone who hasn't learnt the colours. */
  .plugsub { position: absolute; top: 14px; right: 26px; font-size: 11px;
             font-family: var(--mono); font-weight: 500; color: var(--dim);
             display: flex; align-items: center; gap: 5px; }
  .plugsub .sdot { width: 7px; height: 7px; border-radius: 50%; background: var(--dim); }
  .plugsub.on { color: var(--text); }
  .plugsub.on .sdot { background: var(--green); animation: pulse 2.4s infinite; }
  .plugsub.ready { color: var(--dim); }  /* the dot says plugged in; the word need not shout */
  .plugsub.ready .sdot { background: var(--blue); }
  .plugsub.bad { color: var(--red); }
  .plugsub.bad .sdot { background: var(--red); }
  .soc-track {
    height: 8px; border-radius: 2px; border: 1px solid var(--line); margin-top: 13px; overflow: hidden;
    background: #080b0f repeating-linear-gradient(90deg, rgba(255,255,255,.05) 0 1px, transparent 1px 11px);
  }
  .soc-fill { height: 100%; background: linear-gradient(90deg, #12633a, var(--green)); box-shadow: 0 0 12px rgba(47,224,122,.55); transition: width .6s ease; }
  .soc-fill.solar { background: linear-gradient(90deg, #7a5200, var(--amber)); box-shadow: 0 0 12px rgba(255,178,36,.55); }
  /* [hidden] must beat the display:grid/flex classes below, or hiding the PV
     rows at night would do nothing. */
  [hidden] { display: none !important; }
  /* Thin bar for the per-plane PV rows - thinner than the SOC track so the
     tile's primary bar keeps its rank. */
  .bar5 { height: 5px; border-radius: 2px; border: 1px solid var(--line); overflow: hidden;
          background: #080b0f repeating-linear-gradient(90deg, rgba(255,255,255,.05) 0 1px, transparent 1px 11px); }
  /* Per-plane PV rows: each fill is that string's share of its plane's DC
     nameplate, so the pair reads as morning/afternoon/cloud at a glance. */
  .pvrows { margin-top: 12px; display: grid; gap: 7px; }
  .pvrow { display: grid; grid-template-columns: 20px minmax(0, 1fr) 52px; align-items: center; gap: 7px; }
  .pvrow .lbl, .pvrow .w { color: var(--dim); font-size: 11px; font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .pvrow .w { text-align: right; }
  .pvrow .fill { height: 100%; width: 0; background: linear-gradient(90deg, #66801f, var(--lime));
                 box-shadow: 0 0 10px rgba(195,245,60,.5); transition: width .6s ease; }
  .split { display: flex; }
  .split .seg { height: 100%; transition: width .6s ease; }
  /* Import is hatched, export solid, so the split survives colour blindness. */
  .split .seg.in {
    background: repeating-linear-gradient(45deg, rgba(0,0,0,.36) 0 2px, transparent 2px 5px),
                linear-gradient(90deg, #7a5200, var(--amber));
    box-shadow: 0 0 10px rgba(255,178,36,.45);
  }
  .split .seg.out { background: linear-gradient(90deg, var(--green), #12633a); box-shadow: 0 0 10px rgba(47,224,122,.45); }
  .flow { display: flex; justify-content: space-between; gap: 8px; white-space: nowrap; }
  .flow .i { color: var(--amber); }
  .flow .o { color: var(--green); }
  section {
    background: linear-gradient(180deg, var(--panel-hi), var(--panel)); border: 1px solid var(--line);
    border-radius: 4px; margin-bottom: 14px; overflow: hidden; --ac: var(--dim);
  }
  section.s-sun { --ac: var(--amber); }
  section.s-bolt { --ac: var(--lime); }
  section.s-clock { --ac: var(--green); }
  /* With timed charging off, the windows and the rate are configuration for a
     scheme that is not running - dim both sections so they read as inert. */
  body.tou-off .s-clock .body, body.tou-off .s-bolt .body { opacity: .45; filter: saturate(.4); }
  section.s-mode { --ac: var(--violet); }
  section.s-diag { --ac: var(--blue); }
  .head {
    padding: 12px 16px; border-bottom: 1px solid var(--line-soft); border-left: 3px solid var(--ac);
    background: rgba(255,255,255,.015);
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
    position: relative;
  }
  .head h2 { margin: 0; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .14em;
             display: flex; align-items: center; gap: 9px; }
  .head h2 .ico { width: 15px; height: 15px; color: var(--ac); }
  .reg { color: var(--dim); font-size: 11px; font-family: var(--mono); letter-spacing: .04em; white-space: nowrap; }
  .body { padding: 16px 18px; }
  .row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 13px 0; border-bottom: 1px solid var(--line-soft); }
  .row:last-child { border-bottom: 0; padding-bottom: 0; }
  .row:first-child { padding-top: 0; }
  .row .name { flex: 1 1 200px; }
  .row .name small { display: block; color: var(--dim); font-size: 12px; margin-top: 2px; }
  summary:focus-visible {
    outline: none; box-shadow: 0 0 0 3px rgba(42,212,238,.25);
  }
  /* The dot pulses forever and the bars slide on every sweep; neither carries
     information that the static state does not. */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; }
  }
  /* Five tiles at 196px fit one row above ~1068px and three below ~860px. In
     between, auto-fit lands on four and orphans the fifth; force three. */
  @media (min-width: 861px) and (max-width: 1067px) {
    .grid { grid-template-columns: repeat(3, 1fr); }
  }
  /* Six tiles fit one row only past ~1276px viewport; between there and the
     rule above, auto-fit lands 5+1 or 4+2. Force the balanced 3x2. */
  @media (min-width: 1068px) and (max-width: 1275px) {
    .grid.six { grid-template-columns: repeat(3, 1fr); }
  }
  @media (max-width: 700px) {
    .row .name { flex: 1 1 100%; }
    .head { flex-wrap: wrap; }
    /* Register addresses are desk-side reference; on a phone they read as
       broken math ("43143 + (n-1)x8") and cost a head line each. */
    .head .reg { display: none; }
    /* The four daylight weather blocks don't fit a phone (328px wanted,
       ~284px given) and the kWh headline + sky line already carry the story -
       drop them rather than cramming them 2x2. */
    .fcwrap .fcright { display: none; }  /* base .fcright rule is later in the sheet */
  }
  .tag {
    font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .09em;
    font-family: var(--mono); padding: 5px 9px; border-radius: 2px; border: 1px solid var(--line); color: var(--dim);
  }
  .tag.on { border-color: rgba(180,139,255,.5); color: var(--violet); background: rgba(180,139,255,.1); }
  .tag.good { border-color: rgba(47,224,122,.5); color: var(--green); background: rgba(47,224,122,.1); }
  .modes { display: flex; gap: 8px; flex-wrap: wrap; }
  /* One panel, two day rows: Today and Tomorrow are the same information for
     consecutive days, so position and label separate them, not colour. */
  .fcday + .fcday { border-top: 1px solid var(--line-soft); margin-top: 16px; padding-top: 16px; }
  /* These stand in for the panel titles the merge removed, so they carry the
     same weight as a section header; only the date stays dim. */
  .fcdaylbl { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .14em;
              color: var(--text); margin-bottom: 6px; }
  .fcdaylbl .d { font-family: var(--mono); font-weight: 400; letter-spacing: .05em; margin-left: 4px;
                 color: var(--dim); }
  .fcbig { font-size: 26px; color: var(--ac);
           text-shadow: 0 0 20px color-mix(in srgb, currentColor 32%, transparent); }
  .fcsky { font-size: 13px; margin-top: 6px; }
  .blk.past { opacity: .45; }
  .fcwrap { display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
  .fcleft { flex: 0 1 270px; min-width: 235px; }
  .fcright { flex: 3 1 440px; display: flex; gap: 8px; }
  .blk {
    flex: 1 1 0; min-width: 76px; text-align: center; padding: 10px 4px 12px;
    border: 1px solid var(--line-soft); border-radius: 3px; background: rgba(255,255,255,.022);
  }
  .blk-h { font-family: var(--mono); font-size: 10px; letter-spacing: .06em; color: var(--dim); }
  .blk-i { width: 34px; height: 34px; display: block; margin: 8px auto 6px; }
  .blk-kwh { font-family: var(--mono); font-size: 17px; font-weight: 600; color: var(--amber);
             font-variant-numeric: tabular-nums; }
  .blk-kwh span { font-size: 10px; color: var(--dim); font-weight: 400; margin-left: 3px; }
  .blk-t { font-family: var(--mono); font-size: 11px; color: var(--dim); margin-top: 4px; }
  .blk-t .wet { color: var(--blue); }
  .timeline {
    position: relative; height: 40px; border: 1px solid var(--line); border-radius: 3px; margin: 14px 0 7px; overflow: hidden;
    background: #080b0f repeating-linear-gradient(90deg, rgba(255,255,255,.055) 0 1px, transparent 1px 4.1667%);
  }
  .span {
    position: absolute; top: 0; bottom: 0;
    background: linear-gradient(180deg, rgba(195,245,60,.5), rgba(195,245,60,.14));
    border-left: 1px solid var(--lime); border-right: 1px solid var(--lime);
  }
  /* Car dispatches are blue to match the Car tile; amber read as a variant of
     the charge window. */
  .span.car {
    top: 60%; bottom: 0;
    background: repeating-linear-gradient(45deg, rgba(77,163,255,.55) 0 4px, rgba(77,163,255,.16) 4px 8px);
    border-left: 1px solid var(--blue); border-right: 1px solid var(--blue);
  }
  .now { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--amber); box-shadow: 0 0 10px var(--amber); }
  .ticks { display: flex; justify-content: space-between; color: var(--dim); font-size: 10px;
           font-family: var(--mono); letter-spacing: .06em; }
  .legend { display: flex; gap: 18px; align-items: center; color: var(--dim); font-size: 11px; margin-top: 10px; }
  .legend i { display: inline-block; width: 15px; height: 9px; margin-right: 7px; vertical-align: -1px; }
  .legend i.win { background: linear-gradient(180deg, rgba(195,245,60,.5), rgba(195,245,60,.14));
                  border-left: 1px solid var(--lime); border-right: 1px solid var(--lime); }
  .legend i.cur { width: 2px; background: var(--amber); box-shadow: 0 0 8px var(--amber); }
  .legend i.car { height: 5px;
    background: repeating-linear-gradient(45deg, rgba(77,163,255,.55) 0 4px, rgba(77,163,255,.16) 4px 8px);
    border-left: 1px solid var(--blue); border-right: 1px solid var(--blue); }
  .calc {
    color: var(--dim); font-size: 13px; line-height: 1.45; margin-top: 3px;
    padding: 7px 11px; border-radius: 3px; background: rgba(195,245,60,.045);
    border: 1px solid var(--line-soft); border-left: 2px solid rgba(195,245,60,.45);
  }
  .calc b { color: var(--lime); font-family: var(--mono); font-weight: 600; }
  .rateval { font-family: var(--mono); font-size: 20px; font-weight: 600; color: var(--lime);
             font-variant-numeric: tabular-nums; margin-left: auto; }
  details { margin-top: 4px; }
  summary { color: var(--dim); font-size: 11px; font-family: var(--mono); text-transform: uppercase;
            letter-spacing: .1em; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; font-family: var(--mono); font-size: 12px; }
  td { padding: 5px 8px; border-bottom: 1px solid var(--line-soft); color: var(--dim); }
  td:first-child { color: var(--cyan); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Solis<span class="hi">Power Management</span></h1>
    <span class="sub"><span class="dot" id="dot"></span><span id="status">connecting…</span></span>
  </header>

  <div class="grid" id="tilegrid">
    <div class="card pv" id="pvcard">
      <div class="label"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg><span>Solar now</span></div>
      <div class="value"><span id="pv">--</span><span class="unit">W</span></div>
      <div class="pvrows" id="pvrows">
        <div class="pvrow"><span class="lbl" id="pvlbl1"></span><div class="bar5"><div class="fill" id="pvfill1"></div></div><span class="w" id="pvw1">--</span></div>
        <div class="pvrow"><span class="lbl" id="pvlbl2"></span><div class="bar5"><div class="fill" id="pvfill2"></div></div><span class="w" id="pvw2">--</span></div>
      </div>
      <div class="note" id="pvnight" hidden>&nbsp;</div>
    </div>
    <div class="card batt">
      <div class="label"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="7" width="16" height="10" rx="2"/><path d="M22 11v2"/><path d="M5.5 10v4"/></svg><span>Battery</span></div>
      <div class="value"><span id="soc">--</span><span class="unit">%</span></div>
      <div class="soc-track"><div class="soc-fill" id="socfill" style="width:0%"></div></div>
      <div class="note mrow"><span class="k" id="bdir">--</span><span class="v acc" id="bflow">--</span></div>
      <div class="note mrow"><span class="k" id="bvi">--</span><span class="v" id="bdetail">--</span></div>
    </div>
    <div class="card house">
      <div class="label"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9.5V21h13V9.5"/></svg><span>House load</span></div>
      <div class="value"><span id="load">--</span><span class="unit">W</span></div>
      <div class="note" id="hday">--</div>
    </div>
    <div class="card day">
      <div class="label"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5v3M5.2 7.2l2.1 2.1M2.5 15h2.6M18.8 7.2l-2.1 2.1M21.5 15h-2.6"/><path d="M7.8 15a4.2 4.2 0 0 1 8.4 0"/><path d="M2.5 19.5h19"/></svg><span>Solar today</span></div>
      <div class="value"><span id="today">--</span><span class="unit">kWh</span></div>
      <div class="soc-track"><div class="soc-fill solar" id="todayfill" style="width:0%"></div></div>
      <div class="note" id="todaynote">--</div>
    </div>
    <div class="card mains">
      <div class="label"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 2v6M15 2v6M6 8h12v2.5a6 6 0 0 1-12 0z"/><path d="M12 16.5V22"/></svg><span>Grid</span></div>
      <div class="value vrow" id="gvalue"><span id="gpower">--</span><span class="unit">W</span><span class="vsub"><span id="grid">--</span> V</span></div>
      <div class="soc-track split"><div class="seg in" id="gin" style="width:0%" title="imported today"></div><div class="seg out" id="gout" style="width:0%" title="exported today"></div></div>
      <div class="note flow" id="gday">--</div>
      <div class="note flow" id="gmoney" hidden></div>
    </div>
    {% if car_enabled %}
    <div class="card car" id="carcard" hidden>
      <div class="label"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5.5 12.5 7 8.3A2 2 0 0 1 8.9 7h6.2A2 2 0 0 1 17 8.3l1.5 4.2M5.5 12.5h13a1.5 1.5 0 0 1 1.5 1.5v3h-2.1M5.5 12.5A1.5 1.5 0 0 0 4 14v3h2.1m0 0a1.7 1.7 0 1 0 3.4 0m-3.4 0h3.4m5 0a1.7 1.7 0 1 0 3.4 0m-3.4 0H9.5"/></svg><span>Car</span></div>
      <div class="plugsub" id="carplug" hidden><span class="sdot"></span><span id="carplugword"></span></div>
      <div class="value"><span id="carvalue">--</span><span class="unit" id="carunit"></span></div>
      <div class="note" id="carrows">--</div>
    </div>
    {% endif %}
  </div>

  <section class="s-sun">
    <div class="head"><h2><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6"/></svg><span>Sun</span></h2></div>
    <div class="body">
      <div class="fcday" id="todayrow" hidden>
        <div class="fcwrap">
          <div class="fcleft">
            <div class="fcdaylbl">Today <span class="d" id="tddate"></span></div>
            <div class="value fcbig" id="tdkwh">--</div>
            <div class="fcsky" id="tdsummary">--</div>
          </div>
          <div class="fcright" id="tdblocks"></div>
        </div>
      </div>
      <div class="fcday">
        <div class="fcwrap">
          <div class="fcleft">
            <div class="fcdaylbl">Tomorrow <span class="d" id="fcdate"></span></div>
            <div class="value fcbig" id="fckwh">--</div>
            <div class="fcsky" id="fcsummary">--</div>
            <div class="note" id="fcverdict"></div>
          </div>
          <div class="fcright" id="fcblocks"></div>
        </div>
      </div>
    </div>
  </section>

  <section class="s-clock">
    <div class="head"><h2><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/></svg><span>Charge windows</span></h2><span class="tag" id="touword" title="Timed charging — 43110 bit1">--</span><span class="reg">43143 + (n−1)&times;8</span></div>
    <div class="body">
      <div class="timeline" id="timeline"></div>
      <div class="ticks"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
      <div class="legend"><span><i class="win"></i>charge window</span><span><i class="cur"></i>now</span><span id="carleg" hidden><i class="car"></i>car charge</span></div>
      <div class="note car" id="car" hidden><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5.5 12.5 7 8.3A2 2 0 0 1 8.9 7h6.2A2 2 0 0 1 17 8.3l1.5 4.2M5.5 12.5h13a1.5 1.5 0 0 1 1.5 1.5v3h-2.1M5.5 12.5A1.5 1.5 0 0 0 4 14v3h2.1m0 0a1.7 1.7 0 1 0 3.4 0m-3.4 0h3.4m5 0a1.7 1.7 0 1 0 3.4 0m-3.4 0H9.5"/></svg><span id="cartext"></span></div>
      <div id="slots" style="margin-top:14px"></div>
      <details style="margin-top:12px">
        <summary>Slots 2 and 3 — deliberately unused</summary>
        <div id="slots23"></div>
      </details>
    </div>
  </section>

  <section class="s-bolt">
    <div class="head"><h2><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/></svg><span>Charge current</span></h2><span class="reg">43141</span></div>
    <div class="body">
      <div class="row">
        <div class="name">Rate<small id="chargewhen">Set with control.py; shown here read-only.</small></div>
        <span class="rateval" id="chargeval">--</span>
      </div>
      <div class="calc" id="chargecalc">&nbsp;</div>
    </div>
  </section>

  <section class="s-mode">
    <div class="head"><h2><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/><circle cx="9" cy="7" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="8" cy="17" r="2"/></svg><span>Work mode</span></h2><span class="reg">43110 = <span id="modeval">--</span></span></div>
    <div class="body"><div class="modes" id="modes"></div>
      <div class="note">Read-only here — flipping mode bits blind is how you end up exporting or off-grid.</div>
    </div>
  </section>

  <section class="s-diag">
    <div class="head"><h2><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2.5 7 4-15 2.5 8h5"/></svg><span>Diagnostics</span></h2></div>
    <div class="body">
      <table style="margin-top:0">
        <tr><td>logger</td><td id="host">--</td></tr>
        <tr><td>last sweep</td><td id="age">--</td></tr>
        <tr><td>poll interval</td><td>{{ poll }} s inverter &middot; 5 s browser</td></tr>
      </table>
      <details style="margin-top:12px">
        <summary>Raw battery block {{ raw_from }}–{{ raw_to }}</summary>
        <table id="raw"></table>
      </details>
      <div class="note">The browser reads a cached sweep; only this process talks to the logger, and it never writes &mdash; control.py is the only write path.</div>
    </div>
  </section>
</div>

<script>
let battVolts = null;
let battSoc = null;

// Three Fox LV5200 modules in parallel: ~300 Ah at 51.2 V, ~15.4 kWh. It read
// 5.1 for a while, which is *one* module - every fill estimate came out three
// times too short. Used only to describe what a chosen current means in kW and
// in hours; the dashboard never acts on it.
const BATTERY_KWH = 15.4;

// String 1 is the 12-panel SW plane (12 x 400 W), string 2 the 8-panel SE
// plane (8 x 400 W). The basis is the voltage ratio, not a traced cable: the
// verified string voltages (358.4 V vs 236.0 V) sit in the 12:8 ratio the
// series panel counts demand. If that ever proves wrong, this table is the
// single place to swap. Fills are each string's share of its plane's DC
// nameplate, clamped - a cold clear day can brush it.
const PV_PLANES = {
  1: { label: 'SW', nameplate_w: 4800 },
  2: { label: 'SE', nameplate_w: 3200 },
};

let lastEpoch = 0;
let windowHours = null;
// windowHours is null in two different situations - no sweep has been rendered
// yet, and slot 1 is unset - and they need opposite wording. Without this the
// charge row quoted a fill time for a rate that is not being applied at all,
// directly contradicting the line above it.
let windowKnown = false;

// A cached sweep older than this is stale: the poller has stalled or the
// logger has stopped answering, and the numbers on screen are fiction.
const STALE_SECONDS = 30;

// Tomorrow at a glance: four blocks of daylight, not twenty-four hours. The
// question this panel answers is when the sun arrives and roughly how much of
// it, so each block leads with its own kWh; temperature is a footnote.
const SKY = {
  sun: '<circle cx="12" cy="12" r="4.4" fill="#ffb224"/>'
     + '<g stroke="#ffb224" stroke-width="1.7" stroke-linecap="round">'
     + '<path d="M12 2.8v2.3M12 18.9v2.3M2.8 12h2.3M18.9 12h2.3'
     + 'M5.5 5.5l1.6 1.6M16.9 16.9l1.6 1.6M18.5 5.5l-1.6 1.6M7.1 16.9l-1.6 1.6"/></g>',
  partly: '<circle cx="15.8" cy="8" r="3.3" fill="#ffb224"/>'
     + '<g stroke="#ffb224" stroke-width="1.4" stroke-linecap="round">'
     + '<path d="M15.8 2.2v1.6M21.6 8h1.6M19.9 3.9l1.1-1.1M19.9 12.1l1.1 1.1"/></g>'
     + '<path d="M6.6 19.4h8.8a3.4 3.4 0 0 0 .2-6.8 5.1 5.1 0 0 0-9.7 1.2 2.9 2.9 0 0 0 .7 5.6z" fill="#aebecd"/>',
  cloud: '<path d="M6.6 18.6h8.8a3.4 3.4 0 0 0 .2-6.8 5.1 5.1 0 0 0-9.7 1.2 2.9 2.9 0 0 0 .7 5.6z" fill="#93a5b6"/>',
  rain: '<path d="M6.6 15.6h8.8a3.4 3.4 0 0 0 .2-6.8 5.1 5.1 0 0 0-9.7 1.2 2.9 2.9 0 0 0 .7 5.6z" fill="#8496a6"/>'
     + '<g stroke="#4da3ff" stroke-width="1.9" stroke-linecap="round">'
     + '<path d="M8.7 18.2v2.4M12 18.6v2.6M15.3 18.2v2.4"/></g>',
};

// When the array is strongest - the timing only, deliberately with no kW figure.
// The magnitude here is not quotable: hourly_kw() scales irradiance by
// EFFECTIVE_KWP, which is fitted to daily *energy* (nameplate 8.0 x PR 0.79) and
// so reads about 20% low as an instantaneous power, and the five-model mean
// flattens the peak on top of that - tomorrow the models disagree 3.5 to 5.6 kW.
// Both effects leave the daily total right and the peak wrong, and a number that
// contradicts the inverter's own display discredits the kWh figure beside it.
// The timing survives the disagreement, so that is all this line claims.
const STRONG_FRACTION = 0.8;

function peakLine(kw) {
  if (!kw || !kw.length) { return ''; }
  let best = 0;
  for (let h = 1; h < kw.length; h++) {
    if (kw[h] > kw[best]) { best = h; }
  }
  if (kw[best] <= 0) { return ''; }
  // Walk out from the peak rather than filtering the whole day, so a bright
  // hour either side of a dull afternoon cannot stretch the span across it.
  const floor = kw[best] * STRONG_FRACTION;
  let first = best;
  let last = best;
  while (first > 0 && kw[first - 1] >= floor) { first--; }
  while (last < kw.length - 1 && kw[last + 1] >= floor) { last++; }
  // End exclusive, matching dayBlocks: hours 11..15 are strong *through* 16:00,
  // and the two lines sit inches apart claiming the same clock.
  const at = h => String(h % 24).padStart(2, '0') + ':00';
  return first === last ? 'strongest around ' + at(best)
                        : 'strongest ' + at(first) + '\u2013' + at(last + 1);
}

function skyOf(cloud, rain) {
  if (rain >= 40) { return 'rain'; }
  if (cloud >= 75) { return 'cloud'; }
  if (cloud >= 35) { return 'partly'; }
  return 'sun';
}

function dayBlocks(kw, weather, nowHour) {
  const hourly = weather && weather.hourly;
  if (!kw || !kw.length || !hourly) { return ''; }
  const clock = s => { const p = String(s || '').split(':'); return p.length === 2 ? (+p[0]) + (+p[1]) / 60 : null; };
  const rise = clock(weather.sunrise), set = clock(weather.sunset);
  const from = rise === null ? 6 : Math.round(rise);
  const to = set === null ? 21 : Math.round(set);
  const width = Math.max(1, Math.round((to - from) / 4));
  const pad = h => String(h % 24).padStart(2, '0');
  const mean = list => list.reduce((a, b) => a + b, 0) / list.length;

  let out = '';
  for (let b = 0; b < 4; b++) {
    const start = from + b * width;
    const end = b === 3 ? to : start + width;
    if (start >= end || start > 23) { continue; }
    const hours = [];
    for (let h = start; h < Math.min(end, 24); h++) { hours.push(h); }
    const cloud = mean(hours.map(h => hourly.cloud[h]));
    const rain = Math.max.apply(null, hours.map(h => hourly.rain[h]));
    const temp = Math.max.apply(null, hours.map(h => hourly.temp[h]));
    const kwh = hours.reduce((a, h) => a + kw[h], 0);
    // On the Today panel a block that has fully passed is history, not
    // forecast - dim it so the eye lands on what is still to come.
    out += '<div class="blk' + (nowHour !== undefined && end <= nowHour ? ' past' : '') + '">'
         + '<div class="blk-h">' + pad(start) + '–' + pad(end) + '</div>'
         + '<svg class="blk-i" viewBox="0 0 24 24">' + SKY[skyOf(cloud, rain)] + '</svg>'
         + '<div class="blk-kwh">' + kwh.toFixed(1) + '<span>kWh</span></div>'
         + '<div class="blk-t">' + Math.round(temp) + '°'
         + (rain >= 10 ? ' <span class="wet">' + Math.round(rain) + '%</span>' : '')
         + '</div></div>';
  }
  return out;
}

// Whether the last sweep that rendered was itself good. Staleness is time-based
// and can be recomputed at any moment; this cannot, so it is remembered.
let lastOk = false;

// render() only runs when a poll comes back. A hung fetch, a suspended laptop,
// or a background tab whose timers the browser has throttled all leave the last
// verdict standing indefinitely - so the page could present an hour-old reading
// as live. Re-judge freshness on the clock instead.
function staleNow() {
  if (!lastOk) { return true; }
  if (!lastEpoch) { return true; }
  const age = Date.now() / 1000 - lastEpoch;
  return age > STALE_SECONDS || age < -STALE_SECONDS;
}

function watchdog() {
  if (staleNow()) {
    document.body.classList.add('stale');
    document.getElementById('dot').className = 'dot bad';
    document.getElementById('status').textContent = 'no fresh reading';
  }
}

// Today's grid £, on its own line under the kWh line - the two together
// overflow the tile. Import includes the standing charge: it is a cost of
// being connected, and a day of zero import still costs it.
function gridMoney(cost) {
  const row = document.getElementById('gmoney');
  const hasImp = cost && cost.import_gbp != null;
  const hasExp = cost && cost.export_gbp != null;
  row.hidden = !(hasImp || hasExp);
  if (row.hidden) { return; }
  const title = cost.standing_p != null
    ? ' title="includes ' + cost.standing_p.toFixed(0) + 'p standing charge; '
      + cost.cheap_kwh.toFixed(1) + ' kWh off-peak, ' + cost.peak_kwh.toFixed(1) + ' kWh peak"' : '';
  row.innerHTML =
    (hasImp ? '<span class="i"' + title + '>↓ &pound;' + cost.import_gbp.toFixed(2) + '</span>' : '<span></span>')
    + (hasExp ? '<span class="o">&pound;' + cost.export_gbp.toFixed(2) + ' ↑</span>' : '');
}

const mrow = (k, v, acc) =>
  '<div class="mrow"><span class="k">' + k + '</span><span class="v'
  + (acc ? ' acc' : '') + '">' + v + '</span></div>';

function render(s) {
  // Polls overlap, so responses can arrive out of order. Never let an older
  // sweep overwrite a newer one - after a write that would silently roll the
  // displayed settings back to their pre-write values.
  const epoch = typeof s.read_at_epoch === 'number' ? s.read_at_epoch : null;
  if (epoch !== null) {
    if (epoch < lastEpoch) {
      return;
    }
    // A sweep stamped in the future is untrustworthy, so it must not be
    // recorded as the newest one seen. One NTP step forward would otherwise
    // park lastEpoch an hour ahead and drop every legitimate sweep behind it,
    // freezing the page as stale until the clock caught up.
    if (!(epoch > Date.now() / 1000 + STALE_SECONDS)) {
      lastEpoch = epoch;
    }
  }
  const dot = document.getElementById('dot');
  const status = document.getElementById('status');
  const age = epoch === null ? null : Math.round(Date.now() / 1000 - epoch);
  // Distrust a sweep from the future as much as an old one: both mean the
  // timestamp cannot be used to judge freshness.
  const stale = age !== null && (age > STALE_SECONDS || age < -STALE_SECONDS);
  lastOk = s.ok;
  document.body.classList.toggle('stale', stale || !s.ok);
  document.getElementById('host').textContent = s.host || '--';
  document.getElementById('age').textContent =
    (s.read_at || '--') + (age === null ? '' : ' · ' + age + ' s ago');
  if (!s.ok) {
    dot.className = 'dot bad';
    status.textContent = (s.error || 'no data') + ' — retrying';
    return;
  }
  dot.className = stale ? 'dot bad' : 'dot live';
  status.textContent = !stale ? 'read at ' + s.read_at
    : age < 0 ? 'sweep timestamp is ahead of this clock — treating as stale'
    : 'stale — last read ' + age + ' s ago';

  const now = new Date();

  const t = s.telemetry;
  document.getElementById('soc').textContent = t.soc;
  document.getElementById('socfill').style.width = t.soc + '%';
  document.getElementById('pv').textContent = t.pv_power;

  // Daylight bounds for *today*, so a low reading after dark reads as nightfall
  // rather than a fault. Falls back to treating it as daytime.
  // One row of sub-detail: dim label, mono figure, hard against opposite edges.
  const clockNow = String(now.getHours()).padStart(2, '0') + ':'
                 + String(now.getMinutes()).padStart(2, '0');
  const sun = (s.forecast && s.forecast.weather && s.forecast.weather.today) || null;
  const daylight = sun && sun.sunrise && sun.sunset
    ? (clockNow >= sun.sunrise && clockNow < sun.sunset) : true;
  document.getElementById('pvcard').classList.toggle('night', !daylight);
  // By day the tile carries one utilisation bar per roof plane; by night the
  // bars would be two empty tracks, so they yield to the sunrise/sunset line.
  document.getElementById('pvrows').hidden = !daylight;
  document.getElementById('pvnight').hidden = daylight;
  if (daylight) {
    for (const n of [1, 2]) {
      // Looked up by the string's register index, never by array position:
      // dead strings are skipped server-side, and the planes wake and fade at
      // different times.
      const plane = PV_PLANES[n];
      const live = (t.pv_strings || []).find(p => p.string === n);
      const watts = live ? live.watts : 0;
      document.getElementById('pvlbl' + n).textContent = plane.label;
      document.getElementById('pvfill' + n).style.width =
        Math.min(100, watts / plane.nameplate_w * 100) + '%';
      document.getElementById('pvw' + n).textContent = watts + ' W';
    }
  } else if (sun) {
    // Before dawn the next sunrise is today's; after sunset it is tomorrow's,
    // which lives at the top level of the weather payload rather than under
    // .today. Quoting today's 05:53 at midnight would name a moment that has
    // already been and gone.
    const wx = (s.forecast && s.forecast.weather) || {};
    const preDawn = clockNow < sun.sunrise;
    const rise = preDawn ? sun.sunrise : (wx.sunrise || sun.sunrise);
    document.getElementById('pvnight').innerHTML = preDawn
      ? mrow('sunrise', rise) + mrow('sunset', sun.sunset)
      : mrow('sunset', sun.sunset) + mrow('sunrise', rise);
  }

  const flow = document.getElementById('bflow');
  const bdir = document.getElementById('bdir');
  if (t.battery_power < 10) {
    bdir.textContent = 'idle';
    flow.textContent = '';
    flow.style.color = '';
  } else {
    bdir.textContent = t.battery_charging ? 'charging' : 'discharging';
    flow.textContent = (t.battery_charging ? '\u2191 ' : '\u2193 ') + t.battery_power + ' W';
    flow.style.color = t.battery_charging ? 'var(--accent)' : 'var(--amber)';
  }
  battVolts = t.battery_voltage;
  battSoc = t.soc;
  // Volts and amps belong together as one measurement, so they stay paired on
  // the label side; health is the figure, and it moves about once a year.
  document.getElementById('bvi').textContent =
    t.battery_voltage.toFixed(1) + ' V \u00b7 ' + t.battery_current.toFixed(1) + ' A';
  document.getElementById('bdetail').textContent = t.soh + '% health';
  document.getElementById('load').textContent = t.house_load;
  document.getElementById('hday').innerHTML =
    mrow('today', t.house_today.toFixed(1) + ' kWh')
    + mrow('yesterday', t.house_yesterday.toFixed(1) + ' kWh');
  document.getElementById('grid').textContent = t.grid_voltage.toFixed(1);

  const gpower = document.getElementById('gpower');
  const gvalue = document.getElementById('gvalue');
  if (Math.abs(t.grid_power) < 25) {
    gpower.textContent = '0';
    gvalue.style.color = '';
  } else if (t.grid_power > 0) {
    gpower.textContent = '\u2193 ' + t.grid_power;
    gvalue.style.color = 'var(--amber)';
  } else {
    gpower.textContent = '\u2191 ' + Math.abs(t.grid_power);
    gvalue.style.color = 'var(--accent)';
  }
  // Today's import against export, as a share of the two rather than a pair of
  // numbers - the split is the thing worth seeing at a glance.
  const traded = t.grid_import_today + t.grid_export_today;
  document.getElementById('gin').style.width =
    (traded ? t.grid_import_today / traded * 100 : 0) + '%';
  document.getElementById('gout').style.width =
    (traded ? t.grid_export_today / traded * 100 : 0) + '%';
  // A 0.1 kWh trickle against a 20 kWh day renders under a pixel and reads as
  // "none at all". A nonzero share keeps at least a visible sliver; the flex
  // track shrinks the other segment to make room.
  document.getElementById('gin').style.minWidth = t.grid_import_today > 0 ? '3px' : '0';
  document.getElementById('gout').style.minWidth = t.grid_export_today > 0 ? '3px' : '0';
  document.getElementById('gday').innerHTML =
    '<span class="i">↓ ' + t.grid_import_today.toFixed(1) + ' kWh</span>'
    + '<span class="o">' + t.grid_export_today.toFixed(1) + ' kWh ↑</span>';
  gridMoney(s.cost);

  const f = s.forecast || {};
  const expected = f.today_kwh === null || f.today_kwh === undefined ? null : f.today_kwh;
  const sofar = t.solar_today;
  document.getElementById('today').textContent = sofar === undefined ? '--' : sofar.toFixed(1);
  document.getElementById('todayfill').style.width =
    (expected && sofar !== undefined ? Math.min(100, sofar / expected * 100) : 0) + '%';
  // While the sun is up this is a progress bar; once the day has closed it is a
  // result, and "of ~30 kWh expected" wrongly implies there is more to come.
  // The label carries what the prose used to say in words: while the sun is up
  // this is a target still being chased, and once the day has closed it is a
  // settled result. Losing that distinction would leave "-4.0 kWh" reading as
  // "behind schedule" at 21:00, when it actually means "finished behind".
  // "Not daylight" is two different states. After sunset the day is a settled
  // result; before sunrise it has not happened yet, and the counter reading 0
  // is not a shortfall. Caught live at 23:54 local, when the inverter's own
  // clock - a few minutes fast - rolled the daily counters and the tile
  // announced "day closed, -30.1 kWh".
  // Keyed on the counter, not the clock. Before sunrise the day has not
  // happened yet, but so has it not when the *inverter's* clock rolls the daily
  // counters ahead of ours - caught live at 23:55 local, counter already reset,
  // tile announcing "day closed, -30.1 kWh". Either way a zero counter is not a
  // 30 kWh shortfall, and no clock comparison can tell the two apart.
  const notStarted = sofar === undefined || sofar < 0.1;
  let dayRow;
  if (expected === null) {
    dayRow = mrow('forecast', 'unavailable');
  } else if (daylight && !notStarted) {
    dayRow = mrow('of expected', '~' + expected.toFixed(0) + ' kWh');
  } else if (notStarted) {
    dayRow = mrow('forecast', '~' + expected.toFixed(0) + ' kWh');
  } else {
    const miss = sofar - expected;
    dayRow = Math.abs(miss) < 0.5
      ? mrow('day closed', 'on forecast')
      : mrow('day closed', (miss < 0 ? '\u2212' : '+')
             + Math.abs(miss).toFixed(1) + ' kWh');
  }
  document.getElementById('todaynote').innerHTML = dayRow
    + (t.solar_yesterday === undefined ? ''
       : mrow('yesterday', t.solar_yesterday.toFixed(1) + ' kWh'));

  document.getElementById('fcverdict').textContent = f.text || '';
  const w = f.weather;
  const set = (id, value) => { document.getElementById(id).textContent = value; };
  const cap = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : null;
  const big = (id, kwh) => { document.getElementById(id).innerHTML =
      kwh === null || kwh === undefined ? '--'
      : kwh.toFixed(0) + '<span class="unit">kWh predicted</span>'; };
  const sky = (summary, kw) =>
      [cap(summary), peakLine(kw) || null].filter(Boolean).join(' · ') || '--';
  set('fcdate', f.date || '');
  big('fckwh', f.kwh);
  set('fcsummary', w ? sky(w.summary, f.hourly_kw) : 'weather unavailable');
  document.getElementById('fcblocks').innerHTML = dayBlocks(f.hourly_kw, w);

  // Today: same row shape, no verdict - the verdict is about tonight's
  // window and belongs to tomorrow.
  const tw = w && w.today;
  const todayrow = document.getElementById('todayrow');
  if (tw && tw.hourly && f.today_hourly_kw) {
    todayrow.hidden = false;
    set('tddate', f.today_date || '');
    big('tdkwh', f.today_kwh);
    set('tdsummary', sky(tw.summary, f.today_hourly_kw));
    document.getElementById('tdblocks').innerHTML =
      dayBlocks(f.today_hourly_kw, tw, new Date().getHours());
  } else {
    todayrow.hidden = true;
  }

  const g = s.settings;
  document.getElementById('modeval').textContent = g.mode;
  document.getElementById('modes').innerHTML = g.mode_bits
    .map(b => '<span class="tag on">' + b + '</span>').join('') || '<span class="tag">none</span>';

  // Timed-charging status mirrors 43110 bit1 - display only.
  const touOn = (g.mode >> 1 & 1) === 1;
  document.body.classList.toggle('tou-off', !touOn);
  const touword = document.getElementById('touword');
  touword.textContent = touOn ? 'timed charging on' : 'timed charging off';
  touword.className = 'tag' + (touOn ? ' good' : '');

  // Both the description and the fill-time estimate quote the live window, not
  // the 23:30–05:30 it usually holds: it has already been 04:30 and 03:30.
  const win = (g.slots || []).find(w => w.slot === 1 && !w.unset);
  windowHours = win ? windowSpan(win) : null;
  windowKnown = true;
  document.getElementById('chargewhen').textContent = win
    ? 'Window is ' + win.start + '–' + win.end
      + ' (cheap rate, and it holds the battery against the Tesla).'
    : 'No charge window is set, so this rate is not being applied.';

  document.getElementById('chargeval').textContent = g.charge_current + ' A';
  chargeCalc(g.charge_current);

  const charge = g.slots;

  const nowPct = ((now.getHours() * 60 + now.getMinutes()) / 1440) * 100;
  let bars = '';
  for (const w of charge) {
    if (w.unset) continue;
    // A window that ends before it starts wraps past midnight; draw both halves.
    const ranges = w.end_minutes >= w.start_minutes
      ? [[w.start_minutes, w.end_minutes]]
      : [[w.start_minutes, 1440], [0, w.end_minutes]];
    for (const [a, b] of ranges) {
      bars += '<div class="span" style="left:' + (a / 1440 * 100) + '%;width:'
           + ((b - a) / 1440 * 100) + '%" title="slot ' + w.slot + ' ' + w.start + '–' + w.end + '"></div>';
    }
  }
  // Car dispatches share the track but sit in its lower half, so an overnight
  // slot inside the charge window shows both rather than painting over it.
  const car = s.car;
  for (const d of (car && car.spans) || []) {
    const ranges = d.end_minutes >= d.start_minutes
      ? [[d.start_minutes, d.end_minutes]]
      : [[d.start_minutes, 1440], [0, d.end_minutes]];
    for (const [a, b] of ranges) {
      bars += '<div class="span car" style="left:' + (a / 1440 * 100) + '%;width:'
           + ((b - a) / 1440 * 100) + '%" title="' + d.label + '"></div>';
    }
  }
  bars += '<div class="now" style="left:' + nowPct + '%" title="now"></div>';
  document.getElementById('timeline').innerHTML = bars;
  document.getElementById('carleg').hidden = !(car && car.spans && car.spans.length);

  // Car row: absent from the payload means Octopus is not configured, so the
  // row hides rather than nagging about credentials that were never set up.
  const carRow = document.getElementById('car');
  carRow.hidden = !car;
  if (car) {
    let text = car.text;
    // A daytime dispatch matters because the house battery can end up feeding
    // the car. State what the battery is doing right now next to it - a fact,
    // not an inference about where the energy goes.
    const t = s.telemetry;
    if (car.charging && car.daytime && t) {
      const bp = t.battery_power;
      text += ' · house battery ' + (bp < 10 ? 'idle'
        : (t.battery_charging ? 'charging ' : 'discharging ')
          + (bp >= 1000 ? (bp / 1000).toFixed(1) + ' kW' : bp + ' W'));
    }
    document.getElementById('cartext').textContent = text;
    carRow.classList.toggle('charging', !!car.charging);
    carRow.classList.toggle('daytime', !!car.daytime);
  }
  renderCar(car, t);

  document.getElementById('slots').innerHTML =
    charge.filter(w => w.slot === 1).map(slotRow).join('');
  document.getElementById('slots23').innerHTML =
    charge.filter(w => w.slot !== 1).map(slotRow).join('');

  const raw = s.raw_battery_block || {};
  document.getElementById('raw').innerHTML = Object.entries(raw)
    .map(([k, v]) => '<tr><td>' + k + '</td><td>' + v + '</td></tr>').join('');
}

// The Car tile. Headline is a state word or a start time, never a number this
// system does not have: Octopus gives no car SOC and no live charger power,
// and inferring "car charging" from a large grid draw is exactly the class of
// derived signal this repo has been burned by.
function renderCar(car, t) {
  const card = document.getElementById('carcard');
  if (!card) { return; }  // no key configured: the card was never rendered
  // Unplugged is the common, empty case: hide the tile and let the row fall
  // back to five-across. Anomalies (no link, unknown, unavailable) stay
  // visible - they mean something is wrong, which is worth a glance.
  const show = !(car && car.ok && car.plug === 'unplugged');
  card.hidden = !show;
  document.getElementById('tilegrid').classList.toggle('six', show);
  if (!show) { return; }
  const money = g => g < 1 ? Math.round(g * 100) + 'p' : '\u00a3' + g.toFixed(2);
  const value = document.getElementById('carvalue');
  const unit = document.getElementById('carunit');
  const plug = document.getElementById('carplug');
  card.classList.remove('night');
  card.style.removeProperty('--ac');
  let note = '\u00a0';
  if (!car || !car.ok) {
    card.classList.add('night');
    value.textContent = '--';
    unit.textContent = '';
    plug.hidden = true;
    note = 'unavailable';
  } else {
    // The headline is the target - the same number+unit grammar as the other
    // five tiles. The plug state rides the top-right corner instead.
    if (car.target) {
      value.textContent = car.target.soc;
      unit.textContent = '% by ' + car.target.time;
    } else {
      value.textContent = '--';
      unit.textContent = '';
    }
    plug.hidden = false;
    document.getElementById('carplugword').textContent = car.plug;
    plug.className = 'plugsub ' + (car.charging ? 'on'
      : car.plug === 'plugged in' ? 'ready'
      : car.plug === 'no link' ? 'bad' : '');
    if (!car.charging && car.plug !== 'plugged in') {
      card.classList.add('night');  // unplugged / no link / unknown: dim like Solar after sunset
    }
    // Label-left, figure-right rows like every other tile (see .mrow): the
    // dot-joined sentence wrapped at tile width and orphaned "· ~61p" on a
    // line of its own (2026-08-23).
    const rows = [];
    if (car.charging) {
      rows.push(mrow('charging', car.until ? 'until ' + car.until : 'now', true));
      if (car.daytime) {
        // A daytime dispatch is the house-battery-feeds-car risk: amber, with
        // the battery's measured behaviour alongside (a fact, not an inference).
        card.style.setProperty('--ac', 'var(--amber)');
        if (t && !t.battery_charging && t.battery_power >= 10) {
          rows.push(mrow('battery', '\u2193' + t.battery_power + ' W'));
        }
      } else if (car.active_kwh) {
        rows.push(mrow('slot', '+' + car.active_kwh + ' kWh'));
      }
    } else if (car.next) {
      rows.push(mrow('next', car.next.day + ' ' + car.next.start, true));
      const plan = [];
      if (car.planned_kwh) { plan.push('~' + car.planned_kwh + ' kWh'); }
      if (car.planned_gbp) { plan.push('~' + money(car.planned_gbp)); }
      if (plan.length) {
        rows.push(mrow('plan', plan.join(' \u00b7 ')));
      }
    } else if (car.plug === 'plugged in') {
      rows.push(mrow('plan', 'none'));
    }
    if (rows.length) { note = rows.join(''); }
  }
  const box = document.getElementById('carrows');
  if (note.startsWith('<')) { box.innerHTML = note; } else { box.textContent = note; }
}

function slotRow(w) {
  return '<div class="row"><div class="name">Slot ' + w.slot
    + '<small class="reg">' + w.addr + (w.unset ? ' \u00b7 unset' : '') + '</small></div>'
    + '<span class="rateval">' + (w.unset ? '\u2014' : w.start + ' \u2013 ' + w.end) + '</span></div>';
}

// Length of a window in hours, counting one that wraps past midnight.
function windowSpan(w) {
  const mins = w.end_minutes > w.start_minutes
    ? w.end_minutes - w.start_minutes
    : 1440 - w.start_minutes + w.end_minutes;
  return mins / 60;
}

// What the configured rate actually buys, at the battery's live voltage and
// state of charge. Stated, not recommended - the rate is the owner's call.
//
// A rate too low to fill the pack used to report the time it *would* take -
// "about 58 h, longer than the 6 h window". That is true and useless: the window
// is fixed, so the question is never how long a full charge takes, it is what
// state of charge the morning starts at. Below the fill rate the line answers in
// percent instead; at or above it, time-to-full is the meaningful number again.
function chargeCalc(amps) {
  const el = document.getElementById('chargecalc');
  if (isNaN(amps) || amps <= 0 || !battVolts || battVolts <= 0 || battSoc === null) {
    el.innerHTML = '&nbsp;';
    return;
  }
  const kw = amps * battVolts / 1000;
  const missing = BATTERY_KWH * (100 - battSoc) / 100;
  let text = '<b>' + amps + ' A</b> at ' + battVolts.toFixed(1) + ' V is <b>'
           + kw.toFixed(1) + ' kW</b>';
  const hours = missing / kw;
  if (missing < 0.1) {
    text += ' &middot; battery is already full';
  } else if (windowKnown && windowHours === null) {
    // Slot 1 is unset, so this rate is not being applied to anything. Quoting a
    // fill time here contradicted the line directly above, which says exactly
    // that no window is set.
    text += ' &middot; no charge window is set, so this rate does nothing';
  } else if (windowHours === null) {
    // No sweep yet - describe the rate on its own and claim nothing about when.
    text += ' &middot; ' + missing.toFixed(1) + ' kWh to fill from ' + battSoc + '%';
  } else if (hours <= windowHours) {
    text += ' &middot; fills from ' + battSoc + '% in about <b>' + duration(hours)
          + '</b>, inside the ' + duration(windowHours) + ' window';
  } else {
    const gained = kw * windowHours / BATTERY_KWH * 100;
    // This branch is only reached when the rate *cannot* fill the pack, so the
    // rounded figure must never say 100 - it read "reaching 100% - 0.0 kWh
    // short of full" in one sentence, densest right around the ~48 A fill rate
    // and the low overnight SOC this line exists to be read at.
    const reached = Math.min(99, Math.round(battSoc + gained));
    const short = missing - kw * windowHours;
    // Rounding the shortfall to one decimal printed "0.0 kWh short of full" for
    // a rate that misses by minutes - a zero quantity in a sentence whose whole
    // job is to say the pack does not fill. Name it instead of measuring it.
    text += ' &middot; the ' + duration(windowHours) + ' window adds <b>'
          + (gained < 1 ? 'under 1%' : '+' + Math.round(gained) + '%')
          + '</b>, reaching <b>' + reached + '%</b> — '
          + (short < 0.05 ? 'just short of full'
                          : short.toFixed(1) + ' kWh short of full');
  }
  el.innerHTML = text;
}

function duration(hours) {
  const mins = Math.round(hours * 60);
  if (mins < 60) {
    return mins + ' min';
  }
  const rest = mins % 60;
  return Math.floor(mins / 60) + ' h' + (rest ? ' ' + rest + ' min' : '');
}

async function tick() {
  try {
    const res = await fetch('/api/state');
    render(await res.json());
  } catch (err) {
    document.getElementById('dot').className = 'dot bad';
    document.getElementById('status').textContent = 'dashboard unreachable';
    document.body.classList.add('stale');
  }
}

tick();
setInterval(tick, 5000);
// Faster than the poll, because its job is to notice that the poll has stopped.
setInterval(watchdog, 2000);
</script>
</body>
</html>"""
