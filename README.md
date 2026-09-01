# Varmeopt

Home Assistant add-on der optimerer varmekilder efter elpris: varmepumpe,
pillefyr og elpatroner på en ACthor 9S, planlagt sammen med Predbats batteriplan.

Anlægget er en 16 kW Nibe varmepumpe, et 16 kW pillefyr, 2×500 L parallelle
akkumuleringstanke, solvarme og elpatroner, styret hydraulisk af en TA UVR16.
Node-RED er protokol-gateway mellem UVR'en, varmepumpen og Home Assistant.

**Formålet** er at flytte varmeproduktionen hen hvor strømmen er billig — lade
tankene op i ét sammenhængende blok når prisen er lav, så den lagrede varme
dækker de dyre timer og aftenens varme vand.

## Status: fase 0

Add-on'en **styrer intet endnu.** Den har overtaget COP-læringen og gør den
efterprøvelig. Node-RED træffer fortsat alle beslutninger indtil fase 4.

Hvad der virker nu:

- Migrerer den indlærte COP-tabel ud af Node-REDs context til `/data` — 333
  celler og godt 17.000 målinger, med de defekte `NaN`-nøgler renset fra.
- Lærer videre i sin egen tabel, med et delta-T-afhængigt plausibilitetsfilter
  i stedet for det flade 1,0–7,0 Node-RED bruger.
- Slår COP op med **rettet 2D-interpolation.** Node-RED-udgaven satte
  `count: 0` på alt interpoleret, hvorfor blandingsgrenene aldrig udløste og
  opslaget faldt tilbage på fabrikkens TA-kurve overalt undtagen ved eksakte
  celletræf. Her føres et effektivt målingsantal med gennem interpolationen.
- Web-UI gennem HA's ingress: aktuel COP med hele begrundelseskæden, og et
  varmekort over den indlærte tabel hvor huller i dækningen er synlige.

## Installation

Add-on'en installeres som et eget add-on-repository:

1. **Indstillinger → Tilføjelser → Tilføjelsesbutik → ⋮ → Repositories**
2. Tilføj URL'en til dette repo
3. Installér **Varmeopt** og start den

Den henter selv COP-tabellen fra Node-RED første gang den starter. Node-RED
røres ikke — der læses kun.

### Indstillinger

| Nøgle | Standard | Betydning |
|-------|----------|-----------|
| `cycle_seconds` | 60 | Hvor ofte der læres og slås op |
| `nodered_url` | `http://192.168.1.159:1880` | Node-REDs admin-API |
| `entity_flow_temp` | `sensor.node_1_analog_logging_13` | Fremløbstemperatur |
| `entity_cop_measured` | `sensor.node_1_analog_logging_12` | Målt COP fra UVR'en |
| `entity_outdoor_temp` | *(tom)* | Udetemperatur. Er den tom, læses `udeTemp` fra Node-REDs flow-context, hvor MQTT-værdien fra Nibe lander i dag |

## Udvikling

Ingen afhængigheder ud over `aiohttp`. Testene bruger kun standardbiblioteket:

```bash
cd addon
python -m unittest discover -s tests -t .
```

`cop.py` er ren beregning uden I/O og er dækket af tests — det er hele grunden
til at logikken ligger her og ikke i Node-RED-funktionsnoder: en fortegnsfejl i
varmeøkonomi koster penge og opdages først efter dage, så den skal kunne fanges
af en test i stedet for af en regning.

Kørsel uden for HA (læser Node-RED, publicerer intet):

```bash
cd addon
VARMEOPT_NODERED_URL=http://192.168.1.159:1880 python -m varmeopt
```

## Opbygning

| Fil | Ansvar |
|-----|--------|
| `cop.py` | COP-tabel, læring, 2D-interpolation. Ren, testet |
| `store.py` | Atomisk JSON-lager i `/data` |
| `nodered.py` | Read-only klient mod Node-REDs admin-API |
| `migrate.py` | Engangsflytning af COP-tabellen |
| `ha.py` | HA REST-klient via `supervisor/core` |
| `web.py` | Web-UI gennem ingress |
| `__main__.py` | Hovedløkken |

Kommende faser tilføjer `prices.py` (Predbats marginalpriser),
`thermal.py` (tankenergi), `demand.py` (varmebehov), `planner.py`
(blokplanlægning) og `guard.py` (sessionslås og sikkerhed).
