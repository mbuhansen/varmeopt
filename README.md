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
  i stedet for det flade 1,0–7,0 Node-RED bruger. Hver måling tælles **én
  gang**: add-on'en poller, hvor Node-RED lyttede på hændelser, så uden det
  ville en stillestående aflæsning blive lært om igen hvert minut og `count`
  tælle minutter i stedet for målinger.
- Slår COP op med **rettet 2D-interpolation.** Node-RED-udgaven satte
  `count: 0` på alt interpoleret, hvorfor blandingsgrenene aldrig udløste og
  opslaget faldt tilbage på fabrikkens TA-kurve overalt undtagen ved eksakte
  celletræf. Her føres et effektivt målingsantal med gennem interpolationen.
- Læser de otte tankfølere og regner lageret om til **kWh brugbar varme** over
  radiatorkredsens fremløb, samt hvor meget plads der er tilbage op til loftet.
  Tre dybdefølere pr. tank gør lagdelingen synlig — en tank med 58 °C i toppen
  og 31 °C i bunden rummer noget helt andet end en der er 58 °C hele vejen ned,
  og en enkelt temperatur kan ikke skelne. Falder en føler ud, dækker de
  resterende lag tanken i stedet for at tælle som iskolde.
- Fører forskellen mellem de to tankes middeltemperatur med. To parallelle
  tanke bør lagdele ens; gør de ikke det, er det flowfordelingen der er skæv,
  ikke varmen.
- Web-UI gennem HA's ingress: aktuel COP med hele begrundelseskæden, et
  varmekort over den indlærte tabel hvor huller i dækningen er synlige, og en
  lagerside der tegner begge tanke lag for lag.

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
| `entity_tank_a_*` / `entity_tank_b_*` | *(udfyldt)* | Tre dybdefølere pr. tank — `top`, `mid`, `bottom` — plus `outlet` på afgangsrøret. Rækkefølgen bærer betydning: lagdelingen kan ikke regnes uden at vide hvilken føler der sidder hvor |
| `tank_liters` | 1000 | Samlet volumen, fordelt ligeligt på tankene |
| `tank_reference_temp` | 30 | Under den er varmen ikke til nogen nytte — radiatorkredsen kører på godt 31 °C |
| `tank_max_temp` | 60 | Loftet der regnes plads op til |
| `auto_update` | `false` | Hent nyeste kode fra master ved hver opstart |

## Opdatering

Supervisor er langsom til at opdage et push, og under udvikling er ventetiden
den dyreste del af en rettelse. Fanen **System** i web-UI'et henter derfor
Python-koden direkte fra master og starter add-on'en forfra — uden at
Supervisor skal bygge et image.

Koden lægges i `/data/code`, som står før `/app` på `PYTHONPATH`, så en hentet
udgave vinder over den indbyggede. Siden viser hvilken af de to der kører.

**Det dækker kun Python-koden.** Nye indstillinger i `config.yaml`, nye pakker
i `requirements.txt` og ændringer i Dockerfilen hører til imaget og kræver
stadig en almindelig opdatering gennem butikken.

To sikkerhedsnet: den hentede kode oversættes før der skiftes til den, så en
halv commit aldrig bliver det der starter, og der sættes et mærke før
genstarten som først ryddes når web-UI'et er oppe. Findes mærket ved opstart,
nåede sidste forsøg aldrig frem, og forrige udgave rulles tilbage automatisk —
ellers ville en ImportError sende add-on'en i genstartsløkke.

`auto_update` gør det samme ved hver opstart. Den er slået fra som
udgangspunkt: det kører kode fra internettet uden et menneske imellem, og den
der kan pushe til master, kan køre kode i containeren.

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
| `tank.py` | Lagerets fysik: lagdeling, energi, plads. Ren, testet |
| `selfupdate.py` | Henter kode fra master, med oversættelses- og boot-kontrol |
| `store.py` | Atomisk JSON-lager i `/data` |
| `nodered.py` | Read-only klient mod Node-REDs admin-API |
| `migrate.py` | Engangsflytning af COP-tabellen |
| `ha.py` | HA REST-klient via `supervisor/core` |
| `web.py` | Web-UI gennem ingress |
| `__main__.py` | Hovedløkken |

Kommende faser tilføjer `prices.py` (Predbats marginalpriser), `demand.py`
(varmebehov), `planner.py` (blokplanlægning) og `guard.py` (sessionslås og
sikkerhed). Tankenergien, der stod på listen som `thermal.py`, ligger nu i
`tank.py`.
