"""Web-UI, serveret gennem Home Assistants ingress.

Ingress betyder at HA proxyer siden ind under sin egen sti og står for login,
så der aabnes ingen port paa netvaerket. Til gengaeld kender vi ikke vores egen
sti-praefiks, og **alle links skal derfor vaere relative**.

Grafik tegnes som ren HTML og inline SVG. Ingen matplotlib: der er ingen grund
til at rendere billeder paa serveren naar browseren kan tegne selv, og en
Alpine-container skal ikke slaebe rundt paa den afhaengighed.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from aiohttp import web

from . import VERSION, selfupdate
from .cop import CopTable
from .curve import HeatCurve

PORT = 8099

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --fg: #1a1a18; --muted: #6b6b66;
  --line: #e3e3df; --card: #ffffff; --accent: #b4530a;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#191917; --fg:#e9e9e4; --muted:#9a9a92;
          --line:#333330; --card:#211f1d; --accent:#e0894a; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 20px 22px 60px; }
nav { display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:22px; }
nav a { padding:9px 14px; text-decoration:none; color:var(--muted);
        border-bottom:2px solid transparent; font-weight:500; }
nav a.on { color:var(--fg); border-bottom-color:var(--accent); }
h1 { font-size:19px; margin:0 0 4px; letter-spacing:-.01em; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.07em;
     color:var(--muted); margin:26px 0 10px; font-weight:600; }
.sub { color:var(--muted); margin:0 0 22px; }
.card { background:var(--card); border:1px solid var(--line);
        border-radius:10px; padding:16px 18px; margin-bottom:14px; }
.big { font-size:30px; font-weight:600; letter-spacing:-.02em; }
.badge { display:inline-block; padding:2px 9px; border-radius:20px;
         font-size:12px; font-weight:600; border:1px solid var(--line); }
dl { display:grid; grid-template-columns:auto 1fr; gap:7px 18px; margin:0; }
dt { color:var(--muted); }
dd { margin:0; font-variant-numeric:tabular-nums; }
table { border-collapse:collapse; font-variant-numeric:tabular-nums;
        font-size:11px; }
th, td { border:1px solid var(--line); padding:2px 4px; text-align:center;
         white-space:nowrap; }
th { background:var(--card); font-weight:600; position:sticky; }
thead th { top:0; z-index:2; }
tbody th { left:0; z-index:1; }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:10px;
          background:var(--card); }
.legend { display:flex; gap:14px; align-items:center; color:var(--muted);
          font-size:12px; margin:10px 0 0; flex-wrap:wrap; }
.swatch { display:inline-block; width:13px; height:13px; border-radius:3px;
          vertical-align:-2px; margin-right:5px; border:1px solid #0002; }
code { font:12px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;
       background:#8881; padding:1px 5px; border-radius:4px; }
.warn { color:var(--accent); }
.tanks { display:flex; gap:16px; flex-wrap:wrap; align-items:flex-start; }
.tank { flex:1 1 220px; min-width:200px; }
.tank h3 { font-size:13px; margin:0 0 8px; letter-spacing:.03em; }
.vessel { border:1px solid var(--line); border-radius:10px; overflow:hidden;
          box-shadow:0 1px 2px #0001; }
.layer { padding:15px 13px; color:#fff; display:flex;
         justify-content:space-between; align-items:baseline;
         font-weight:600; font-variant-numeric:tabular-nums; }
.layer span { font-size:12px; letter-spacing:.05em; text-transform:uppercase;
              opacity:.9; font-weight:500; }
.layer b { font-size:17px; font-weight:600; }
.layer.none { background:#8883; color:var(--muted); }
.foot { display:grid; grid-template-columns:auto 1fr; gap:5px 14px;
        margin:10px 2px 0; font-size:12px; }
.foot dt { color:var(--muted); }
.foot dd { margin:0; font-variant-numeric:tabular-nums; }
button { font:inherit; font-weight:600; padding:9px 16px; border-radius:8px;
         border:1px solid var(--line); background:var(--accent); color:#fff;
         cursor:pointer; }
button:hover { filter:brightness(1.08); }
button.plain { background:var(--card); color:var(--fg); }
button:focus-visible, a:focus-visible { outline:2px solid var(--accent);
         outline-offset:2px; }
.actions { display:flex; gap:10px; flex-wrap:wrap; margin:4px 0 16px; }
.note { border-left:3px solid var(--accent); padding:8px 0 8px 14px;
        margin:0 0 16px; }
table.plan { width:100%; font-size:13px; }
table.plan th, table.plan td { border:0; border-bottom:1px solid var(--line);
        text-align:left; padding:6px 10px; }
table.plan thead th { background:var(--card); color:var(--muted);
        font-size:11px; text-transform:uppercase; letter-spacing:.06em; }
table.plan tr.now { background:#8881; }
table.plan td.clock { font-variant-numeric:tabular-nums; white-space:nowrap; }
table.plan td.bar { position:relative; min-width:130px;
        font-variant-numeric:tabular-nums; }
table.plan td.bar span { position:absolute; left:10px; top:50%;
        transform:translateY(-50%); height:16px; border-radius:3px;
        opacity:.22; }
table.plan td.bar b { position:relative; font-weight:600; }
table.plan td.why { color:var(--muted); font-size:12px; }
table.plan td.raw { color:var(--muted); font-variant-numeric:tabular-nums; }
.tag { display:inline-block; margin-left:7px; padding:1px 6px;
        border-radius:10px; font-size:10px; font-weight:600;
        background:var(--fg); color:var(--bg); vertical-align:1px; }
.tag.target { background:var(--accent); color:#fff; }
"""

# Kurvens egen farve. Fast frem for currentColor, fordi linjen er det ene
# element på siden der bærer betydning — valgt så den holder på både lys og
# mørk bund.
_CURVE_INK = "#4a90c2"

# Kilderne har hver sin farve hele vejen gennem UI'et, saa en raekke
# kan laeses paa farven alene.
_SOURCE_INK = {"varmepumpe": "#1f7a4d", "pillefyr": "#b4530a"}

_SOURCE_LABEL = {
    "exact": ("Indlært", "#1f7a4d"),
    "interp": ("Interpoleret", "#2f6ea8"),
    "blend": ("Delvist lært", "#b4530a"),
    "curve": ("TA-kurve", "#8a8a82"),
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _page(title: str, active: str, body: str) -> web.Response:
    nav = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
        for key, href, label in (
            ("now", "./", "Nu"),
            ("tank", "./tank", "Lager"),
            ("plan", "./plan", "Plan"),
            ("curve", "./curve", "Varmekurve"),
            ("cop", "./cop", "COP-tabel"),
            ("system", "./system", "System"),
        )
    )
    doc = (
        "<!doctype html><html lang=da><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)} · Varmeopt</title><style>{_CSS}</style>"
        f"<main><nav>{nav}</nav>{body}</main></html>"
    )
    return web.Response(text=doc, content_type="text/html", charset="utf-8")


def _temp_colour(temp: float) -> str:
    """Blå ved 25 °C, rød ved 65 °C. Lagdelingen skal kunne ses på farven alene."""
    t = max(0.0, min(1.0, (temp - 25.0) / 40.0))
    r = int(56 + (196 - 56) * t)
    g = int(108 + (76 - 108) * t)
    b = int(166 + (56 - 166) * t)
    return f"rgb({r},{g},{b})"


def _cop_colour(cop: float) -> str:
    """Rød ved COP 2, grøn ved COP 5,5. Lineær derimellem."""
    t = max(0.0, min(1.0, (cop - 2.0) / 3.5))
    r = int(203 + (31 - 203) * t)
    g = int(93 + (122 - 93) * t)
    b = int(58 + (77 - 58) * t)
    return f"rgb({r},{g},{b})"


class WebUI:
    def __init__(
        self,
        status: Callable[[], dict[str, Any]],
        table: Callable[[], CopTable],
        port: int = PORT,
        check: Callable[[], Awaitable[Any]] | None = None,
        update: Callable[[], Awaitable[str]] | None = None,
        curve: Callable[[], HeatCurve] | None = None,
    ) -> None:
        self._status = status
        self._table = table
        self._port = port
        self._check = check
        self._update = update
        self._curve = curve
        self._runner: web.AppRunner | None = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self.now)
        app.router.add_get("/tank", self.tank)
        app.router.add_get("/cop", self.cop)
        app.router.add_get("/plan", self.plan)
        app.router.add_get("/curve", self.curve)
        app.router.add_get("/system", self.system)
        app.router.add_post("/system", self.system)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # ------------------------------------------------------------------ sider

    async def now(self, _request: web.Request) -> web.Response:
        s = self._status()
        lookup = s.get("lookup")

        if lookup is None:
            body = (
                "<h1>Venter på første cyklus</h1>"
                f'<p class="sub">{_esc(s.get("note", ""))}</p>'
            )
            return _page("Nu", "now", body)

        label, colour = _SOURCE_LABEL.get(lookup.source, (lookup.source, "#888"))
        learned = (
            f"{lookup.learned_cop:.2f} (n={lookup.learned_count:.1f})"
            if lookup.learned_cop is not None
            else "—"
        )

        rows = [
            ("Fremløb", _fmt(s.get("flow_temp"), "°C")),
            ("Udetemperatur", _fmt(s.get("outdoor_temp"), "°C")),
            ("Målt COP nu", _fmt(s.get("measured_cop"))),
            ("Kilde", f'<span class="badge" style="color:{colour}">{_esc(label)}</span>'),
            ("Opslagsmetode", f"<code>{_esc(lookup.detail)}</code>"),
            ("Lært værdi", _esc(learned)),
            ("Sidste læring", _esc(s.get("learn_note", "—"))),
            ("Sidste cyklus", _esc(s.get("last_run", "—"))),
        ]
        dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)

        body = (
            "<h1>Aktuel COP</h1>"
            f'<p class="sub">{_esc(s.get("note", ""))}</p>'
            f'<div class="card"><div class="big">{lookup.cop:.2f}</div>'
            f'<div class="sub" style="margin:0">{_esc(label)} · {_esc(lookup.detail)}</div></div>'
            f'<h2>Detaljer</h2><div class="card"><dl>{dl}</dl></div>'
            + _price_section(s)
            + _tally_section(s.get("tally"))
        )
        return _page("Nu", "now", body)

    async def tank(self, _request: web.Request) -> web.Response:
        status = self._status()
        buffer = status.get("tank")

        if buffer is None:
            return _page(
                "Lager",
                "tank",
                "<h1>Varmelager</h1><p class='sub'>Ingen af tankfølerne svarer "
                "endnu. Tjek entity-id'erne i add-on-konfigurationen.</p>",
            )

        cards = []
        for t in buffer.tanks:
            bands = []
            for label, temp in (("Top", t.top), ("Midt", t.mid), ("Bund", t.bottom)):
                if temp is None:
                    bands.append(f'<div class="layer none"><span>{label}</span><b>—</b></div>')
                else:
                    bands.append(
                        f'<div class="layer" style="background:{_temp_colour(temp)}">'
                        f"<span>{label}</span><b>{temp:.1f}°</b></div>"
                    )
            foot = [
                ("Afgang", _fmt(t.outlet, "°C", 1)),
                ("Lagdeling", _fmt(t.spread, "K", 1)),
                ("Middel", _fmt(t.mean_temp, "°C", 1)),
                ("Lagret", f"{t.stored_kwh(buffer.reference):.1f} kWh" if t.covered else "—"),
            ]
            cards.append(
                f'<div class="tank"><h3>Tank {_esc(t.name)}</h3>'
                f'<div class="vessel">{"".join(bands)}</div>'
                f'<dl class="foot">'
                + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in foot)
                + "</dl></div>"
            )

        rows = [
            ("Plads til varmepumpe", f"{buffer.headroom_kwh:.1f} kWh"),
            (
                "Plads i alt",
                f"{buffer.peak_headroom_kwh:.1f} kWh"
                f" <span style='color:var(--muted)'>solvarme og ACthor,"
                f" op til {buffer.peak_ceiling:.0f} °C</span>",
            ),
            ("Fyldning", _fmt(buffer.charge_percent, "%", 0)),
            ("Middeltemperatur", _fmt(buffer.mean_temp, "°C", 1)),
            ("Leverer op til", _fmt(buffer.deliverable, "°C", 1)),
            ("Ubalance mellem tanke", _fmt(buffer.imbalance, "K", 1)),
            ("Følere der svarer", f"{buffer.sensor_count} af {3 * len(buffer.tanks)} i dybden"),
            ("Regnet over", f"{buffer.reference:.0f}–{buffer.ceiling:.0f} °C"),
        ]
        dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)

        # Ubalance over 5 K mellem to parallelle tanke er ikke et varmeproblem
        # men et flowproblem, og det er værd at sige højt.
        warn = ""
        if buffer.imbalance is not None and buffer.imbalance > 5:
            warn = (
                f'<p class="legend warn">Tankene står {buffer.imbalance:.1f} K fra '
                "hinanden. To parallelle tanke bør lagdele ens — så stor en forskel "
                "peger på skæv flowfordeling, ikke på varmen.</p>"
            )

        body = (
            "<h1>Varmelager</h1>"
            f'<p class="sub">{buffer.sensor_count} dybdefølere i {len(buffer.tanks)} tanke · '
            f"{sum(t.liters for t in buffer.tanks):.0f} liter</p>"
            f'<div class="card"><div class="big">{buffer.stored_kwh:.1f} kWh</div>'
            '<div class="sub" style="margin:0">brugbar varme over '
            f'{buffer.reference:.0f} °C</div></div>'
            f'<div class="tanks">{"".join(cards)}</div>'
            f'<h2>Samlet</h2><div class="card"><dl>{dl}</dl></div>{warn}'
            + _balance_section(status.get("balance"), buffer)
            + _solar_section(status, buffer)
            + _vessel_section(status)
        )
        return _page("Lager", "tank", body)

    async def plan(self, _request: web.Request) -> web.Response:
        status = self._status()
        rows = status.get("projection") or []
        decision = status.get("decision")

        if not rows:
            return _page(
                "Plan",
                "plan",
                "<h1>Plan</h1><p class='sub'>Ingen plan fra Predbat endnu. "
                "Kildevalget staar stadig — det kraever ingen plan.</p>"
                + (_price_section(status) if status.get("price_now") else ""),
            )

        top = max(r.electricity for r in rows) or 1.0

        # Predbats rækker følger urets hele og halve timer, og den første er
        # den vi står midt i — den er altså kortere end en halv time. Derfor
        # rundes der ned til slottets begyndelse i stedet for at regne fra nu,
        # ellers ville tabellen vise 14:17, 14:47, 15:17.
        clock_now = datetime.now().astimezone()
        start = clock_now.replace(
            minute=0 if clock_now.minute < 30 else 30, second=0, microsecond=0
        )
        left = 30 - (clock_now.minute % 30)

        # Varmepumpen er default. En raekke hvor den bare koerer videre, skal
        # ikke fortaelle en historie om prissaetning - der blev jo ikke gjort
        # noget. Den fulde begrundelse hoerer hjemme hvor noget aendrer sig:
        # naar kilden skifter, naar vi staar i raekken, eller naar den er den
        # planlaeggeren regner imod.
        cells = []
        previous_source = None
        for row in rows:
            changed = previous_source is not None and row.source != previous_source
            previous_source = row.source
            if changed or row.now:
                # Hvorfor den kilde - det er dét der aendrede sig.
                why = row.note
            elif row.target:
                why = "dyreste time — planlæggeren regner herimod"
            else:
                why = _basis(row.reason)
            clock = (start + timedelta(minutes=row.minutes)).strftime("%H:%M")
            width = max(2.0, 100 * row.electricity / top)
            colour = _SOURCE_INK["varmepumpe" if row.source == "varmepumpe" else "pillefyr"]

            mark = ""
            if row.now:
                # Hvor længe der er tilbage af den halvtime vi står i — altså
                # hvor længe prisen holder.
                mark = f'<span class="tag">nu · {left} min</span>'
            elif row.target:
                mark = '<span class="tag target">hertil</span>'

            cells.append(
                f'<tr class="{"now" if row.now else ""}">'
                f"<td class=\"clock\">{clock}{mark}</td>"
                f'<td class="bar"><span style="width:{width:.0f}%;'
                f'background:{colour}"></span>'
                f"<b>{row.electricity:.2f}</b></td>"
                f"<td class=\"raw\">{_fmt(row.import_price, '', 2)}</td>"
                f"<td class=\"raw\">{_fmt(row.export_price, '', 2)}</td>"
                f"<td>{_fmt(row.heat_price, '', 2)}</td>"
                f'<td style="color:{colour};font-weight:600">'
                f"{_esc(row.source)}</td>"
                f'<td class="why">{_esc(why)}</td></tr>'
            )

        head = (
            "<thead><tr><th>Tid</th><th>Marginal</th><th>Import</th>"
            "<th>Eksport</th><th>Varme</th><th>Kilde</th><th>Hvorfor</th>"
            "</tr></thead>"
        )
        summary = _decision_banner(decision)
        hours = rows[-1].minutes / 60

        body = (
            "<h1>Plan</h1>"
            f'<p class="sub">{len(rows)} halvtimer frem, {hours:.0f} timer · '
            "priserne er Predbats, COP fremad regnes på nuværende udetemperatur</p>"
            f"{summary}"
            f'<div class="scroll"><table class="plan">{head}'
            f"<tbody>{''.join(cells)}</tbody></table></div>"
            '<p class="legend">Alle priser i kr/kWh. <b>Marginal</b> er hvad en ekstra kilowatt-time reelt koster i den time — den er hverken import eller eksport, men den af dem der gælder, og «hvorfor» siger hvilken. Bjælken viser den i forhold til den dyreste time i vinduet. «Hertil» markerer den time planlæggeren regner imod.</p>'
        )
        return _page("Plan", "plan", body)

    async def curve(self, _request: web.Request) -> web.Response:
        curve = self._curve() if self._curve is not None else None

        if curve is None or curve.point_count < 2:
            return _page(
                "Varmekurve",
                "curve",
                "<h1>Varmekurve</h1><p class='sub'>For få punkter til at tegne "
                "en kurve endnu.</p>",
            )

        temps = curve.outdoor_temps
        lo, hi = temps[0], temps[-1]
        setpoints = [curve.point(u).setpoint for u in temps]
        ymin = math.floor(min(setpoints)) - 2
        ymax = math.ceil(max(setpoints)) + 2

        width, height = 720, 300
        left, right, top, bottom = 46, 14, 14, 34

        def sx(outdoor: float) -> float:
            return left + (outdoor - lo) / (hi - lo) * (width - left - right)

        def sy(temp: float) -> float:
            return top + (ymax - temp) / (ymax - ymin) * (height - top - bottom)

        grid = []
        for value in range(ymin, ymax + 1):
            if value % 5:
                continue
            y = sy(value)
            grid.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
                'stroke="currentColor" stroke-opacity=".13"/>'
                f'<text x="{left - 7}" y="{y + 3.5:.1f}" text-anchor="end" '
                'font-size="10" fill="currentColor" opacity=".55">'
                f"{value}</text>"
            )

        ticks = []
        for outdoor in temps:
            if outdoor % 5:
                continue
            ticks.append(
                f'<text x="{sx(outdoor):.1f}" y="{height - bottom + 17}" '
                'text-anchor="middle" font-size="10" fill="currentColor" '
                f'opacity=".55">{outdoor}</text>'
            )

        line = " ".join(f"{sx(u):.1f},{sy(curve.point(u).setpoint):.1f}" for u in temps)
        dots = []
        for outdoor in temps:
            point = curve.point(outdoor)
            # Punkter med få målinger tegnes svagt, så tynde steder ses.
            alpha = min(1.0, 0.28 + point.count / 300)
            dots.append(
                f'<circle cx="{sx(outdoor):.1f}" cy="{sy(point.setpoint):.1f}" r="3.2" '
                f'fill="{_CURVE_INK}" opacity="{alpha:.2f}">'
                f"<title>Ude {outdoor} °C: setpunkt {point.setpoint:.1f} °C, "
                f"n={point.count:.0f}</title></circle>"
            )

        chart = (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            'aria-label="UVR-ens varmekurve: fremloebssetpunkt som funktion af '
            'udetemperatur, med antal maalinger bag hvert punkt">'
            f'{"".join(grid)}'
            f'<polyline points="{line}" fill="none" stroke="{_CURVE_INK}" '
            'stroke-width="2" stroke-linejoin="round"/>'
            f'{"".join(dots)}{"".join(ticks)}'
            f'<text x="{left}" y="{height - 4}" font-size="10" fill="currentColor" '
            'opacity=".55">udetemperatur °C</text></svg>'
        )

        # Selve pointen: kurven plus COP-tabellen giver et gæt på COP ved en
        # udetemperatur vi endnu ikke har haft — det en vejrudsigt skal bruge.
        table = self._table()
        rows = []
        for outdoor in range(lo, hi + 1):
            if outdoor % 5:
                continue
            setpoint = curve.predict(outdoor)
            if setpoint is None:
                continue
            lookup = table.lookup(setpoint, outdoor)
            label, colour = _SOURCE_LABEL.get(lookup.source, (lookup.source, "#888"))
            rows.append(
                f"<tr><td>{outdoor} °C</td><td>{setpoint:.1f} °C</td>"
                f"<td>{lookup.cop:.2f}</td>"
                f'<td style="color:{colour}">{_esc(label)}</td>'
                f"<td>{curve.confidence(outdoor):.0f}</td></tr>"
            )

        status = self._status()
        now_rows = [
            ("Udetemperatur nu", _fmt(status.get("outdoor_temp"), "°C", 1)),
            ("Setpunkt nu", _fmt(status.get("flow_temp"), "°C", 1)),
            ("Kurven forudsiger", _fmt(status.get("predicted_setpoint"), "°C", 1)),
            ("Målt fremløb, centralvarme", _fmt(status.get("flow_measured"), "°C", 1)),
            ("Varmepumpe frem, BT12", _fmt(status.get("hp_flow"), "°C", 1)),
            ("Varmepumpe retur, BT3", _fmt(status.get("hp_return"), "°C", 1)),
            ("Løft over kondensator", _fmt(status.get("hp_lift"), "K", 1)),
            ("Tilstand", _esc(status.get("mode") or "—")),
            ("Sidste kurvelæring", _esc(status.get("curve_note") or "—")),
        ]
        dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in now_rows)

        body = (
            "<h1>Varmekurve</h1>"
            f'<p class="sub">{curve.point_count} punkter fra {lo} til {hi} °C ude · '
            f"{curve.sample_count:.0f} målinger bag · varmtvand ved "
            f"{curve.dhw_setpoint:.0f} °C er holdt udenfor</p>"
            f'<div class="card">{chart}</div>'
            f'<h2>Nu</h2><div class="card"><dl>{dl}</dl></div>'
            "<h2>Forudsagt COP</h2>"
            '<div class="scroll"><table style="font-size:13px">'
            "<thead><tr><th>Ude</th><th>Setpunkt</th><th>COP</th>"
            "<th>Kilde</th><th>n</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
            '<p class="legend">Kurven oversætter en vejrudsigt til et fremløb, og '
            "COP-tabellen oversætter fremløbet til en virkningsgrad. Sammen er de "
            "det, en blokplan skal bruge for at vide hvad varmen kommer til at koste "
            "i morgen.</p>"
        )
        return _page("Varmekurve", "curve", body)

    async def system(self, request: web.Request) -> web.Response:
        note = ""
        if request.method == "POST":
            note = (
                await self._update()
                if self._update is not None
                else "Selvopdatering er ikke tilgængelig i denne udgave."
            )

        # GitHub spørges kun når der bliver bedt om det. Et opslag pr.
        # sidevisning ville brænde den uautentificerede kvote på en time.
        latest = "Ikke tjekket"
        if request.query.get("check") and self._check is not None:
            revision = await self._check()
            if revision is None:
                latest = "GitHub svarede ikke"
            elif revision.sha == selfupdate.current():
                latest = f"{revision.short} — du har den nyeste"
            else:
                latest = f"{revision.short} — {_esc(revision.message)}"

        running = selfupdate.current()
        rows = [
            ("Add-on-version", _esc(VERSION)),
            (
                "Kører kode fra",
                "master, hentet her" if selfupdate.running_downloaded() else "add-on-imaget",
            ),
            ("Hentet udgave", _esc(running[:8]) if running else "ingen"),
            ("Nyeste på master", latest),
        ]
        dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)

        body = (
            "<h1>System</h1>"
            '<p class="sub">Hent nyeste kode direkte fra master uden at vente på '
            "at add-on-butikken opdager et push.</p>"
            + (f'<p class="note">{_esc(note)}</p>' if note else "")
            + f'<div class="card"><dl>{dl}</dl></div>'
            '<div class="actions">'
            '<form method="get" action="./system">'
            '<input type="hidden" name="check" value="1">'
            '<button class="plain" type="submit">Tjek master</button></form>'
            '<form method="post" action="./system">'
            '<button type="submit">Hent nyeste og genstart</button></form>'
            "</div>"
            '<p class="legend">Henter kun Python-koden. Nye indstillinger, nye '
            "pakker og ændringer i Dockerfilen hører til add-on-imaget og kræver "
            "stadig en almindelig opdatering gennem butikken.</p>"
        )
        return _page("System", "system", body)

    async def cop(self, _request: web.Request) -> web.Response:
        table = self._table()
        flows = table.flow_temps

        if not flows:
            return _page(
                "COP-tabel", "cop", "<h1>COP-tabel</h1><p class='sub'>Tabellen er tom.</p>"
            )

        outdoors = sorted({o for f in flows for o in table.row(f)})

        head = "".join(f"<th>{o}</th>" for o in outdoors)
        rows = []
        for f in reversed(flows):
            row = table.row(f)
            cells = []
            for o in outdoors:
                cell = row.get(o)
                if cell is None:
                    cells.append("<td></td>")
                    continue
                # Gennemsigtighed efter belægning: tynde celler skal se tynde
                # ud, så hullerne i dækningen springer i øjnene.
                alpha = min(1.0, 0.25 + cell.count / 20)
                cells.append(
                    f'<td style="background:{_cop_colour(cell.cop)};opacity:{alpha:.2f};'
                    f'color:#fff" title="F{f}° U{o}°: COP {cell.cop:.2f}, n={cell.count:.0f}">'
                    f"{cell.cop:.1f}</td>"
                )
            rows.append(f"<tr><th>{f}°</th>{''.join(cells)}</tr>")

        body = (
            "<h1>Indlært COP</h1>"
            f'<p class="sub">{table.cell_count} celler · {table.sample_count:.0f} målinger · '
            f"fremløb {min(flows)}–{max(flows)} °C · ude {min(outdoors)}–{max(outdoors)} °C</p>"
            '<div class="scroll"><table><thead><tr><th>F \\ U</th>'
            f"{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
            '<p class="legend">'
            f'<span><span class="swatch" style="background:{_cop_colour(2.0)}"></span>COP 2,0</span>'
            f'<span><span class="swatch" style="background:{_cop_colour(3.75)}"></span>COP 3,8</span>'
            f'<span><span class="swatch" style="background:{_cop_colour(5.5)}"></span>COP 5,5</span>'
            "<span>Blegere farve = færre målinger bag tallet</span></p>"
        )
        return _page("COP-tabel", "cop", body)


def _basis(reason: str) -> str:
    """Kun hvor prisen kommer fra — «batteri», «net», «eksport».

    Bruges på de rækker hvor intet ændrer sig. Begrundelsen bag prisen er
    stadig rigtig og står på sensoren, men på skærmen ville den støje: den
    ville få en helt almindelig time til at se ud som en beslutning.
    """
    return reason.split(":")[0].strip() or reason


def _tally_section(tally: Any) -> str:
    """Hvor tit vi er uenige med Node-RED, og hvad der stod på spil."""
    if tally is None or tally.compared <= 0:
        return ""

    rows = [
        ("Enige", f"{tally.agreement_percent:.0f} % af {tally.compared:.0f} cyklusser"),
        ("Uenige", f"{tally.disagreed:.0f}"),
        ("… hvor vi ville køre varmepumpe", f"{tally.ours_heatpump:.0f}"),
        ("… hvor vi ville fyre med piller", f"{tally.ours_boiler:.0f}"),
        ("Varme leveret imens", f"{tally.heat_kwh:.1f} kWh"),
        ("På spil", f"{tally.stake_kr:.2f} kr"),
    ]
    if tally.since:
        rows.append(("Målt siden", _esc(tally.since)))

    dl = "".join(f"<dt>{_esc(k)}</dt><dd>{v}</dd>" for k, v in rows)
    return (
        '<h2>Mod Node-RED</h2><div class="card">'
        f"<dl>{dl}</dl></div>"
        '<p class="legend">«På spil» er ikke en bevist besparelse. Det er hvad '
        "<em>vores egne tal</em> siger der er forskel på de to valg — og de tal er "
        "netop det der er til debat. Det er indsatsen i væddemålet, ikke gevinsten. "
        "Men det afgør om det er værd at lade add-on'en styre: står der to kroner om "
        "måneden på spil, er svaret nej.</p>"
    )


def _decision_banner(decision: Any) -> str:
    """Hvad den vil gøre, med det samme og med store bogstaver."""
    if decision is None:
        return ""
    colour = _SOURCE_INK.get(decision.source, "#8a8a82")
    extra = ""
    if decision.charge and decision.charge_kwh is not None:
        extra = (
            f'<div class="sub" style="margin:6px 0 0">Lad {decision.charge_kwh:.1f} kWh '
            f"op nu — {decision.saving_kr:.2f} kr at hente mod om "
            f"{decision.window_minutes} min</div>"
        )
    else:
        extra = '<div class="sub" style="margin:6px 0 0">Lader ikke op</div>'
    return (
        f'<div class="card"><div class="big" style="color:{colour}">'
        f"{_esc(decision.source.upper())}</div>"
        f'<div class="sub" style="margin:0">{_esc(decision.reason)}</div>{extra}</div>'
    )


def _price_section(status: dict[str, Any]) -> str:
    """Marginalprisen nu, hvad varmen koster, og hvad Node-RED ville vælge."""
    price = status.get("price_now")
    if price is None:
        return ""

    heat = status.get("heat_price")
    pellet = status.get("pellet_price")
    decision = status.get("decision")

    rows = [
        ("Marginal elpris", f"{price.kr_per_kwh:.2f} kr/kWh"),
        ("Hvorfor", _esc(price.reason)),
        ("Varmepumpevarme", _fmt(heat, "kr/kWh", 3)),
        ("Pillevarme", _fmt(pellet, "kr/kWh", 3)),
    ]

    plan = status.get("plan")
    if plan is not None:
        for hours in (2, 4, 6):
            ahead = plan.marginal(hours * 60)
            if ahead is not None:
                rows.append(
                    (
                        f"Om {hours} timer",
                        f"{ahead.kr_per_kwh:.2f} kr/kWh "
                        f"<span style='color:var(--muted)'>{_esc(ahead.reason)}</span>",
                    )
                )
        rows.append(("Planens horisont", f"{plan.horizon_minutes / 60:.1f} timer"))

    badge = ""
    if decision is not None:
        colour = "#1f7a4d" if decision.source == "varmepumpe" else "#b4530a"
        badge = (
            f'<div class="card"><div class="big" style="color:{colour}">'
            f"{_esc(decision.source.upper())}</div>"
            f'<div class="sub" style="margin:0">{_esc(decision.reason)}</div></div>'
        )
        rows.append(("Lad op", _esc(decision.charging_note)))
        if decision.saving_kr is not None:
            rows.append(
                (
                    "At hente",
                    f"{decision.saving_kr:.2f} kr mod om {decision.window_minutes} min",
                )
            )

    dl = "".join(f"<dt>{_esc(k)}</dt><dd>{v}</dd>" for k, v in rows)
    return f'<h2>Pris og valg</h2>{badge}<div class="card"><dl>{dl}</dl></div>'


def _balance_section(balance: Any, buffer: Any) -> str:
    """Effekt ind mod effekt ud — og hvor længe det holder."""
    if balance is None:
        return ""

    sources = balance.sources
    rows = [(name.capitalize(), f"{kilowatt:.2f} kW") for name, kilowatt in sources.items()]
    if not rows:
        rows = [("Kilder", "ingen leverer lige nu")]

    load_kw = balance.load.kw
    rows += [
        ("Husets behov", _fmt(load_kw, "kW", 2)),
        (
            "Frem / retur",
            f"{_fmt(balance.load.flow, '°C', 1)} / {_fmt(balance.load.ret, '°C', 1)}"
            f" · {_fmt(balance.load.litres_per_hour, 'l/h', 0)}",
        ),
        ("Netto", _fmt(balance.net_kw, "kW", 2)),
    ]

    if buffer is not None:
        left = balance.hours_left(buffer.stored_kwh)
        full = balance.hours_to_full(buffer.headroom_kwh)
        if left is not None:
            rows.append(("Lageret rækker", f"{left:.1f} timer"))
        elif full is not None:
            rows.append(("Fuldt om", f"{full:.1f} timer"))
        else:
            rows.append(("Vandret", "hverken fyldes eller tømmes nævneværdigt"))

    dl = "".join(f"<dt>{_esc(k)}</dt><dd>{v}</dd>" for k, v in rows)
    free = balance.free_kw
    note = ""
    if free > 0.05:
        note = (
            f'<p class="legend">Heraf {free:.2f} kW fra solvarmen, som er gratis. '
            "En opladning med varmepumpen oven i den fortrænger fri varme med købt.</p>"
        )
    return f'<h2>Effektbalance</h2><div class="card"><dl>{dl}</dl></div>{note}'


def _solar_section(status: dict[str, Any], buffer: Any) -> str:
    """Hvor meget af varmepumpens bånd solen selv tager i dag."""
    expected = status.get("solar_expected")
    if expected is None and status.get("solar_pv_remaining") is None:
        return ""

    def pv(value: Any) -> str:
        return f"<span style='color:var(--muted)'>af {_fmt(value, 'kWh PV', 1)}</span>"

    rows = [
        (
            "Forventet solvarme, resten af i dag",
            f"{_fmt(expected, 'kWh', 1)} {pv(status.get('solar_pv_remaining'))}",
        ),
        (
            "Forventet solvarme i morgen",
            f"{_fmt(status.get('solar_expected_tomorrow'), 'kWh', 1)} "
            f"{pv(status.get('solar_pv_tomorrow'))}",
        ),
        ("Solvarme indtil nu i dag", _fmt(status.get("solar_today"), "kWh", 1)),
    ]

    may = status.get("solar_may_charge")
    worth = status.get("solar_worth_starting")
    if may is not None and buffer is not None:
        verdict = ""
        if worth is False:
            minimum = status.get("solar_min_charge") or 0
            verdict = (
                f" <span style='color:var(--accent)'>for lidt til at starte, "
                f"mindst {minimum:.1f}</span>"
            )
        rows.append(("Varmepumpen må lade", f"{may:.1f} kWh{verdict}"))

    scale = status.get("solar_scale")
    days = status.get("solar_days") or 0
    if scale is not None:
        seen = f"{days:.0f} egne døgn" if days else "startværdi, endnu ingen egne døgn"
        rows.append(("Skalafaktor", f"{scale:.3f} <span style='color:var(--muted)'>{seen}</span>"))

    dl = "".join(f"<dt>{_esc(k)}</dt><dd>{v}</dd>" for k, v in rows)
    note = ""
    if may is not None and buffer is not None:
        if worth is False:
            note = (
                f'<p class="legend">Af varmepumpens {buffer.headroom_kwh:.1f} kWh plads '
                f"venter solen at tage {expected:.1f}, og der er kun {may:.1f} kWh "
                "tilbage. Det fylder varmepumpen på under ét minimumstræk, hvorefter "
                "den slukker igen — og en start der straks følges af et stop er slid "
                "uden udbytte. Lad den stå.</p>"
            )
        else:
            note = (
                f'<p class="legend">Af varmepumpens {buffer.headroom_kwh:.1f} kWh plads '
                f"venter solen at tage {expected:.1f}. Lader varmepumpen mere end "
                f"{may:.1f} kWh, fortrænger den gratis varme — og skal den lade, så "
                "oppefra, så bunden bliver kold nok til at solfangeren kan arbejde.</p>"
            )
    return f'<h2>Solprognose</h2><div class="card"><dl>{dl}</dl></div>{note}'


def _vessel_section(status: dict[str, Any]) -> str:
    """Varmtvandsbeholder og spa: egne lagre, samme varmekilder."""
    rows = [
        ("VVB top", _fmt(status.get("vvb_top"), "°C", 1)),
        ("VVB bund", _fmt(status.get("vvb_bottom"), "°C", 1)),
        ("Spa", _fmt(status.get("spa_temp"), "°C", 1)),
        ("Spa mål", _fmt(status.get("spa_target"), "°C", 1)),
    ]
    heating = status.get("spa_heating")
    if heating is not None:
        rows.append(("Spa varmer", "ja" if heating else "nej"))

    if all(value == "—" for _, value in rows):
        return ""

    dl = "".join(f"<dt>{_esc(k)}</dt><dd>{v}</dd>" for k, v in rows)
    return (
        f'<h2>Brugsvand og spa</h2><div class="card"><dl>{dl}</dl></div>'
        '<p class="legend">Egne lagre ved siden af buffertankene — de deler '
        "varmekilder, men ikke energi, og lægges derfor ikke sammen med dem. "
        "Det er dem der kalder med 56 °C.</p>"
    )


def _fmt(value: Any, unit: str = "", digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}{' ' + unit if unit else ''}"
    return f"{_esc(value)}{' ' + unit if unit else ''}"
