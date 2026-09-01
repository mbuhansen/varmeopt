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
from typing import Any, Callable

from aiohttp import web

from .cop import CopTable

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
"""

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
            ("cop", "./cop", "COP-tabel"),
        )
    )
    doc = (
        "<!doctype html><html lang=da><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)} · Varmeopt</title><style>{_CSS}</style>"
        f"<main><nav>{nav}</nav>{body}</main></html>"
    )
    return web.Response(text=doc, content_type="text/html", charset="utf-8")


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
    ) -> None:
        self._status = status
        self._table = table
        self._port = port
        self._runner: web.AppRunner | None = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self.now)
        app.router.add_get("/cop", self.cop)
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
        )
        return _page("Nu", "now", body)

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


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}{' ' + unit if unit else ''}"
    return f"{_esc(value)}{' ' + unit if unit else ''}"
