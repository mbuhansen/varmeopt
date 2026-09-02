# Varmeopt

Home Assistant add-on der optimerer varmekilder efter elpris: varmepumpe,
pillefyr og elpatroner på en ACthor 9S, planlagt sammen med Predbats batteriplan.

Anlægget er en 16 kW Nibe varmepumpe, et 16 kW pillefyr, 2×500 L parallelle
akkumuleringstanke, solvarme og elpatroner, styret hydraulisk af en TA UVR16.
Node-RED er protokol-gateway mellem UVR'en, varmepumpen og Home Assistant.

**Formålet** er at flytte varmeproduktionen hen hvor strømmen er billig — lade
tankene op i ét sammenhængende blok når prisen er lav, så den lagrede varme
dækker de dyre timer og aftenens varme vand.

## Status: skyggedrift

Add-on'en **styrer intet.** Den regner det samme valg som Node-RED hvert minut,
på rettet COP, og fører regnskab over hvor tit de to er uenige. Node-RED træffer
alle beslutninger.

Køreplanen frem herfra er fire skridt, i den rækkefølge:

1. **Tæl uenighederne.** Uden et tal kan man kun se dem. Regnskabet står på
   Nu-siden: hvor tit, i hvilken retning, og hvad forskellen var værd. Det er dét
   tal der afgør om resten er værd at bygge. ✔ bygget
2. **`guard.py`** — hvad sker der når planen er forældet, HA ikke svarer, en føler
   lyver, eller add-on'en falder. Plus en sessionslås, så to ting ikke kan styre
   samtidig. Skal ligge inden noget som helst overtages.
3. **Én udgang, ikke alle.** Pillefyr/varmepumpe-valget er UVR'ens kanal 1 — én
   digital udgang, som Node-RED allerede skriver. Den kan overtages alene, mens
   Node-RED beholder resten. Går det galt, er det ét signal at rulle tilbage.
4. **Blokopladning.** Den nye evne, og den farligste: at starte varmepumpen når
   ingen har bedt om varme. Bør vente til ståtabet er målt — med to kroners
   margin kan det led vende fortegnet.

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
- Måler **husets varmebehov** direkte, af centralvarmens frem, retur og flow
  efter tankene. Formlen er efterprøvet mod anlæggets eget display: 130 l/h ved
  18,2 K giver 2,72 kW her, hvor UVR'en skriver 2,73. Uden det tal er lagerets
  fyldning ikke til at handle på — 11,6 kWh er to timer eller tyve.
- Stiller de **fire kilder** op mod forbruget: varmepumpe (elforbrug × målt
  COP), solvarme, elpatroner og pillefyr. Så bliver svaret **«lageret rækker
  6,4 timer»** i stedet for en procentdel. Kilderne holdes adskilt, fordi
  solvarme er gratis og resten ikke er: en blokopladning med varmepumpen oven i
  solen fortrænger fri varme med købt.
- Skelner mellem **to lofter over lageret.** Varmepumpen når 60 °C; solvarme og
  ACthor kan presse tankene til 90. «Plads til varmepumpe» og «plads i alt» er
  derfor to forskellige tal, og er tankene allerede over 60, er en
  blokopladning ikke bare unødvendig — den er umulig.
- Regner **marginalprisen** på strøm — nu og i hver halvtime fremad — af
  Predbats plan, læst direkte fra HA. Spotprisen er ikke svaret: strøm fra
  nettet, strøm fra et batteri der alligevel lades billigt om to timer, og
  strøm man kunne have solgt er tre forskellige tal i det samme minut. Node-REDs
  syv prisgrene er generaliseret fra «hvad koster den nu» til «hvad koster den
  kl. 18» — og uden det kan man vælge kilde, men ikke lægge en blok.
- Træffer **det samme valg som Node-RED**, men på den rettede COP, og udstiller
  det side om side. Add-on'en styrer stadig intet; forskellen mellem de to svar
  er præcis det fase 1 skal vurderes på.
- Svarer på **om det betaler sig at lade op på forhånd** — ikke med et døgnskema,
  men med tre spørgsmål hver cyklus: hvilken kilde nu, er der en senere time hvor
  varmen bliver dyrere nok til at dække slitagen, og hvor meget må der så lades
  når solen har fået sit. Et skema forældes i samme øjeblik en pris flytter sig,
  og en styring bruger alligevel kun det første skridt.
- Modellerer **UVR'ens varmekurve**: udetemperatur ind, fremløbssetpunkt ud.
  `sensor.node_1_analog_logging_13` er ikke en måling, men det setpunkt UVR'en
  regner sig frem til — det kan ses direkte i de indlærte data, hvor det falder
  glat fra 49 °C ved −5 °C ude til 24 °C ved +24 °C og så lægger sig fladt.
  Kurven udledes af de 17.176 migrerede målinger og behøver derfor ikke læres
  forfra over uger. Sammen med COP-tabellen oversætter den en vejrudsigt til en
  forventet virkningsgrad — byggestenen under blokplanlægning.
- Holder varmtvand og spa udenfor kurven, og **skelner de to fra hinanden**.
  Anlæggets egne udgange siger det som en kendsgerning — setpunktet på 56 °C
  ville kun være et gæt, og et gæt der fejlede den dag setpunktet blev ændret.
  Tilsammen står de for **7.857 af de 17.176 målinger** — næsten halvdelen af
  varmepumpens drift, og den halvdel med lavest COP. Hvor stor en del der er
  spa frem for brugsvand, viser sig nu i data.
- Læser varmepumpens egne følere, BT12 kondensatorafgang og BT3 retur, og fører
  løftet over kondensatoren med. Et løft nær nul betyder at pumpen ikke laver
  noget, uanset hvad den beregnede COP måtte påstå.
- Læser de otte tankfølere og regner lageret om til **kWh brugbar varme** over
  radiatorkredsens fremløb, samt hvor meget plads der er tilbage op til loftet.
  Tre dybdefølere pr. tank gør lagdelingen synlig — en tank med 58 °C i toppen
  og 31 °C i bunden rummer noget helt andet end en der er 58 °C hele vejen ned,
  og en enkelt temperatur kan ikke skelne. Falder en føler ud, dækker de
  resterende lag tanken i stedet for at tælle som iskolde.
- Fører forskellen mellem de to tankes middeltemperatur med. To parallelle
  tanke bør lagdele ens; gør de ikke det, er det flowfordelingen der er skæv,
  ikke varmen.
- **Plan-siden** viser fremskrivningen halvtime for halvtime — pris, varmepris,
  hvilken kilde der vinder, og hvorfor — med prisbjælker så de dyre timer
  springer i øjnene. «Nu» og «hertil» markerer den time planlæggeren regner
  imod. Den er til at *se* på, ikke til at handle på: styringen spørger
  planlæggeren igen hvert minut og bruger kun svaret for nu. Men uden
  fremskrivningen kan man ikke se hvorfor den svarer som den gør — og så er
  den umulig at stole på.
- Web-UI gennem HA's ingress: aktuel COP med hele begrundelseskæden, et
  varmekort over den indlærte tabel hvor huller i dækningen er synlige, og en
  lagerside der tegner begge tanke lag for lag.

## Strategi: de to bånd

Varmepumpen kan kun løfte tankene til omkring 60 °C. Solvarmen og ACthor når
90. Det deler lageret i to bånd på hver ca. 34,5 kWh, og det er **ikke** ligegyldigt
hvem der tager hvilket.

Lader solen 30–60-båndet først, står varmepumpen tilbage uden plads og kan
ikke bidrage. Lader varmepumpen derimod sit eget bånd mens strømmen er billig,
har solen stadig 60–90 tilbage — som kun den kan nå. To kilder i hvert sit
bånd giver reelt 69 kWh lager i stedet for 34,5.

**Men det står og falder med lagdelingen.** Solvarmens virkningsgrad falder med
kollektortemperaturen, og den arbejder mod tankens bund. Fylder varmepumpen
hele tanken til 60 °C, ser solen 60 °C i bunden i stedet for godt 30, og dens
ydelse kollapser.

Derfor: **lad varmepumpen fylde oppefra, og stop mens bunden stadig er kold.**
Det er den regel de seks dybdefølere er nødvendige for — uden dem kan man ikke
skelne «tanken er 50 °C» fra «toppen er 60, bunden er 35», og det er præcis den
forskel der afgør om solen har noget at arbejde med.

**Hvor meget** varmepumpen må tage, afhænger af hvor meget sol der kommer. Er
dagen overskyet, laver solvarmen ikke nok til at fylde noget bånd, og
varmepumpen kan tage hele 30–60 uden at fortrænge en kilowatt-time. Bliver det
en klar dag, skal der stå plads tilbage.

Det tal kommer fra Solcasts PV-prognose. Solfangerne og
solcellerne ser den samme sol, men ikke fra samme vinkel: fire paneler i syd med
45° hældning mod 6,4 kW syd/20° plus 4 kW vest/15°. Regnet på indfaldsvinklen
svinger forholdet mellem de to flader med en faktor 2,5 hen over året — fra 0,90
i juni til 2,28 i december, fordi 45° møder den lave vintersol nær vinkelret.

En fast omregningsfaktor ville derfor være groft forkert det halve af året. Men
**geometrien kan regnes, ikke læres.** Tilbage står ét enkelt tal: en skalafaktor
for kollektorareal, virkningsgrad og Solcasts egen skævhed.

```
forventet solvarme  =  Solcast kWh  ×  geometrisk forhold(dagen)  ×  k
varmepumpen må lade =  plads til 60 °C  −  forventet solvarme
```

Skalafaktoren er kalibreret på en rigtig dag — 24. august 2026, hvor solcellerne
lavede 60,9 kWh mod solvarmens 29 — og giver **k ≈ 0,43**. Modellen retter selv
tallet efter hvert helt døgn den ser.

Læringen er **med vilje skæv**, og det er den vigtigste beslutning i modellen. En
dag hvor lageret var fuldt, får kollektoren til at holde igen, og målingen bliver
for lav; en dag kan aldrig komme til at yde *mere* end solen gav. Fejlen er
ensidig, så modellen tror hurtigt på en god dag og kun langsomt på en dårlig.

Det er ikke teori. To dage fra anlægget: 24. august gav 60,9 kWh PV og 29 kWh
solvarme med en top på 5,4 kW. Tre dage senere gav 55,4 kWh PV — kun 9 % mindre
— men solvarmen faldt 34 % til 19 kWh, og toppen nåede kun 3,6 kW. Solen var der;
kollektoren fik ikke lov. Symmetrisk læring ville have givet k = 0,397 af de to
dage. Asymmetrisk giver 0,422, mod den frie dags 0,428.

## Sådan skal styringen tænke

Anlægget skal to ting: skifte mellem pillefyr og varmepumpe, og regne ud om det
betaler sig at lade tankene op på forhånd. Det er fristende at bygge dem som to
styringer. Det ville være forkert.

**Kilde-valget er blokplanen med horisonten sat til nul.** «Hvad er billigst lige
nu» er det samme spørgsmål som «hvad skal der ske i det næste minut» — bare uden
fremsyn. To adskilte styringer kan blive uenige (planen siger lad op, øjebliks-
reglen siger pillefyr), og så skal noget tredje afgøre hvem der vinder. Én
planlægger, hvis output også indeholder «kør det her nu», kan ikke komme i
konflikt med sig selv.

Node-REDs `INTELLIGENT VARMESTYRING 2D` *er* allerede den planlægger. Den har
bare nul timers horisont.

### Ligningen for at lade op på forhånd

```
gevinst = E × ( P_aften / COP_aften  −  P_nu / COP_nu )  −  ståtab  −  slid
```

`P_aften` er ikke importprisen, men **eksportprisen**: de kilowatt-timer
varmepumpen bruger om aftenen, er kilowatt-timer der ellers var gået ud gennem
inverteren.

Tre ting gør ligningen mindre skræmmende end den ser ud:

**Brugsvand vender COP-leddet om.** Skal der bades kl. 18, kører varmepumpen
56 °C om aftenen alligevel. Så sammenlignes 56 mod 56, ikke 56 mod varmekurvens
setpunkt — og der er intet tab ved at gemme. Målt på anlæggets egen tabel er
COP'en ved 56 °C endda 4,07 midt på dagen mod 3,86 om aftenen, fordi det er
varmere ude. For-opladning af brugsvand er 5 % *bedre*, ikke 19 % værre.

**Pillefyret sætter et loft.** Varmepumpen om aftenen koster
`eksportpris ÷ COP` pr. kWh varme; pillefyret koster 0,706 kr. Pillefyret vinder
først når eksportprisen passerer **2,72 kr/kWh** — og over det er svaret «brænd
piller og eksportér alt», ikke «lad hurtigere op». Planen skal aldrig jagte
ekstreme priser.

**Men marginen er lille.** Bufferen fra 44 til 56 °C er 13,8 kWh varme, altså
3,4 kWh strøm ved COP 4,07, og den frigør 3,6 kWh inverterkapacitet om aftenen.
Ved 0,50 kr/kWh ind og 1,00 kr/kWh eksport er det knap to kroner. Kommer
middagsstrømmen derimod fra batteriet til dets gennemsnitspris, er det et
nulsumsspil. Derfor er `P_nu` ikke en pris man slår op, men en man udleder af
Predbats plan.

Og derfor er **ståtabet ikke en detalje.** Med to kroners margin kan seks timers
tab fra en 56-graders buffer vende fortegnet. Det er det ene led der endnu ikke
er målt.

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
| `entity_flow_temp` | `sensor.node_1_analog_logging_13` | UVR'ens **beregnede** fremløbssetpunkt. Det er den akse COP-tabellen er indekseret på |
| `entity_flow_measured` | `sensor.node_1_dl_bus_1` | Centralvarmens fremløb ud mod huset, målt **efter** tankene. Forbrugsside, ikke kildeside |
| `entity_hp_flow` | `sensor.nibe_eb101_ep14_bt12_condensor_out` | Varmepumpens kondensatorafgang |
| `entity_hp_return` | `sensor.nibe_eb101_ep14_bt3_return_temp` | Varmepumpens retur |
| `entity_cop_measured` | `sensor.node_1_analog_logging_12` | COP beregnet i UVR'en af to følere og en flowmåler, placeret **før** tankene. Måler derfor varmepumpens egen ydelse, urørt af solvarmen |
| `dhw_setpoint` | 56 | Setpunktet varmtvandsbeholder og spabad kalder med. Målinger derpå holdes ude af varmekurven |
| `entity_outdoor_temp` | *(tom)* | Udetemperatur. Er den tom, læses `udeTemp` fra Node-REDs flow-context, hvor MQTT-værdien fra Nibe lander i dag |
| `entity_tank_a_*` / `entity_tank_b_*` | *(udfyldt)* | Tre dybdefølere pr. tank — `top`, `mid`, `bottom` — plus `outlet` på afgangsrøret. Rækkefølgen bærer betydning: lagdelingen kan ikke regnes uden at vide hvilken føler der sidder hvor |
| `tank_liters` | 1000 | Samlet volumen, fordelt ligeligt på tankene |
| `tank_reference_temp` | 30 | Under den er varmen ikke til nogen nytte — radiatorkredsen kører på godt 31 °C |
| `tank_max_temp` | 60 | Så højt kan varmepumpen presse tankene |
| `tank_peak_temp` | 90 | Anlæggets fysiske loft — solvarme og ACthor når så højt |
| `entity_ch_return`, `entity_ch_flow_rate` | `sensor.node_1_dl_bus_2`, `_3` | Centralvarmens retur og flow. Sammen med fremløbet giver de husets behov i kW |
| `entity_hp_power` | `sensor.node_1_input_15` | Varmepumpens elforbrug. Gange målt COP giver dens varmeydelse |
| `entity_solar_power` | `sensor.solvarme_produktion` | Solvarmens ydelse. **Modelleret**, ikke målt: en flowkurve i UVR'en efter pumpens PWM-signal, uden digital flowmåler |
| `entity_element_power`, `entity_boiler_power` | ACthor, NBE-fyr | Elpatroner og pillefyr melder selv deres effekt |
| `entity_vvb_top`, `entity_vvb_bottom` | `sensor.node_1_input_7`, `_8` | Varmtvandsbeholderen — eget lager ved siden af buffertankene |
| `vvb_liters` | 0 | Beholderens rumfang. Nul betyder «regn ikke energi på den» — to temperaturer er mere ærligt end en kWh-værdi bygget på et gæt |
| `entity_solar_today` | `sensor.solvarme_produktion_idag` | Døgntæller for solvarmen. Sammen med Solcast kalibrerer den skalafaktoren |
| `entity_solcast_*` | Solcast-integrationen | Prognose for resten af i dag og for i morgen |
| `latitude`, `solar_thermal_*`, `pv_a_*`, `pv_b_*` | Fyn, 45° syd, 6,4 kW syd/20° + 4 kW vest/15° | Anlæggets geometri. Årstidsvariationen regnes heraf i stedet for at læres |
| `solar_scale` | 0,43 | Startværdi for skalafaktoren, kalibreret på 24. august 2026. Modellen retter den selv |
| `entity_dhw_active` | `binary_sensor.node_1_output_7` | Kører varmepumpen for brugsvandet? En kendsgerning frem for et gæt ud fra setpunktet |
| `entity_spa_*` | `sensor.tub_temperature` m.fl. | Spabadets tilstand. Det kalder med samme setpunkt som brugsvandet og forklarer hvorfor kurven springer til 56 °C |
| `entity_nodered_decision` | `sensor.varme_styring` | Node-REDs egen beslutning, så de to kan sammenlignes |
| `entity_predbat_plan` | `predbat.plan_html` | Predbats plan. Vi læser `raw.rows`, den strukturerede udgave — ikke HTML-tabellen |
| `pellet_*` | 2,88 kr/kg, 4,8 kWh/kg, 85 % | Pillefyrets pris pr. kWh varme |
| `source_hysteresis` | 0,05 | Så valget ikke vipper frem og tilbage på nogle ører |
| `hp_wear_kr_per_kwh` | 0,15 | At køre varmepumpen koster noget ud over strømmen. Tallet kommer fra den ukoblede `v4`-node, hvor det var en konstant — her kan det efterprøves |
| `planner_horizon_hours` | 12 | Hvor langt frem det giver mening at gemme varme |
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

Tre sikkerhedsnet, fordi en fejl her ellers kun kan rettes fra en terminal:

1. **Koden oversættes før der skiftes til den**, så en halv commit aldrig
   bliver det der starter. Arkivets stier filtreres også — det kommer fra vores
   eget repo, men et tar-arkiv er stadig fremmed input.
2. **Et mærke sættes før genstarten** og ryddes når web-UI'et er oppe. Står det
   der stadig efter tre minutter, er opstarten aldrig lykkedes, og forrige
   udgave hentes frem igen. Henstanden er nødvendig: mærket sættes lige før
   genstarten, så den nye proces finder sit *eget* mærke et sekund senere.
3. **Startskallen `bootstrap.py` ligger i imaget** og hentes aldrig ned. Den
   kører før pakken importeres, så den kan rydde op selv når den hentede kode
   ikke engang kan importeres — det er det tilfælde app'ens egen genopretning
   ikke kan nå at gribe.

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
| `curve.py` | UVR'ens varmekurve: udetemperatur → setpunkt. Ren, testet |
| `demand.py` | Effektbalancen: husets forbrug mod de fire kilder. Ren, testet |
| `prices.py` | Marginalpris pr. halvtime af Predbats plan. Ren, testet |
| `compare.py` | Regnskab over uenigheder med Node-RED. Ren, testet |
| `planner.py` | Kilde nu, og om der skal lades ud over behovet. Ren, testet |
| `solar.py` | Solvarmeprognose af Solcast: geometri regnet, skalafaktor lært |
| `tank.py` | Lagerets fysik: lagdeling, energi, plads. Ren, testet |
| `selfupdate.py` | Henter kode fra master, med oversættelses- og boot-kontrol |
| `bootstrap.py` | Startskal i imaget; rydder op efter en mislykket selvopdatering |
| `store.py` | Atomisk JSON-lager i `/data` |
| `nodered.py` | Read-only klient mod Node-REDs admin-API |
| `migrate.py` | Engangsflytning af COP-tabellen |
| `ha.py` | HA REST-klient via `supervisor/core` |
| `web.py` | Web-UI gennem ingress |
| `__main__.py` | Hovedløkken |

Kommende faser tilføjer `guard.py` (sessionslås og
sikkerhed). Tankenergien, der stod på listen som `thermal.py`, ligger nu i
`tank.py`, og `prices.py` er på plads.
