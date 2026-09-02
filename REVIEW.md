# Kodegennemgang 2. september 2026

Tre uafhængige gennemgange af 0.15.0 — økonomi, fysik og sikkerhed. Alt herunder
er **fundet, ikke rettet.** Rækkefølgen nedenfor er den anbefalede.

Testene kørte grønt (262) gennem hele gennemgangen. Fejlene er dem tests ikke
kan se: forkerte antagelser, forkert fysik, og fejltilstande der aldrig opstår
i en attrap.

**Kør altid testene i venv** — den globale Python har `pytest-homeassistant-custom-component`
installeret, som tvinger socket-blokering og giver 78 falske fejl.

---

## 1. Sikkerhed — skal rettes før styringen kobles til

### 1.1 `styrer: true` bliver hængende for evigt ✱ verificeret
`addon/varmeopt/__main__.py:919-922`

`finally` gemmer tabellen og stopper web-serveren, men sætter aldrig flaget
falsk. En tilstand skrevet med HA's REST-API forsvinder ikke af sig selv.

Dør add-on'en — Supervisor-opdatering, SIGTERM, OOM, en fejl der vælter
processen — står `sensor.varmeopt_beslutning` med `styrer: true` og en frossen
kommando i timer eller dage, og Node-RED følger et lig.

**Ret:** sæt `styrer: false` i `finally`. Overvej også et tidsstempel på
sensoren, så Node-RED selv kan afvise en gammel kommando.

### 1.2 `ha.py` fanger ikke timeout og sætter ingen ✱ verificeret
`addon/varmeopt/ha.py:81, 106, 120`

Fanger kun `aiohttp.ClientError`. `asyncio.TimeoutError` er **ikke** en
`ClientError` (efterprøvet), så en timeout slipper forbi `except HaError` og
vælter cyklussen. Og uden `ClientTimeout` gælder aiohttps standard på **fem
minutter**, mens en cyklus laver ~30 sekventielle opslag.

`addon/varmeopt/nodered.py:51,57` gør det rigtigt. Forskellen er utilsigtet.

**Ret:** `ClientTimeout(total=20)` og fang `TimeoutError`, som i `nodered.py`.

### 1.3 `decide()` prissætter uden måleren ✱ verificeret
`addon/varmeopt/planner.py:176` mod `addon/varmeopt/__main__.py:560`

`decide()` kalder `plan.marginal(0)` **uden** `grid`; sensoren kalder den med.
Hele `Grid`-mekanismen er koblet fra beslutningen. Samme halvtime, import
3,50 kr, tomt batteri, måler viser 9 kW import:

```
sensoren viser  : 3,50 kr/kWh  "net: import"
beslutningen    : 0,80 kr/kWh  "batteri: frit"
```

Rammer også `project()` (`planner.py:262`) og dermed rækken for `minutes=0` i
plan-tabellen — **og uenighedsregnskabet, som derfor måler på en forkert pris.**

**Ret:** giv `decide()` og `project()` `grid` med.

### 1.4 Øvrige sikkerhedsfund
- **Undtagelse i `cycle()` fryser flaget** — `__main__.py:910-914` prøver igen for
  evigt uden tæller. Værre: `_publish_decision` ligger **sidst**, efter fem andre
  skrivninger, så enhver `HaError` afbryder før flaget opdateres.
- **Vagtens ophold nulstilles ved genstart** — `guard.py:54-56` holder kun
  tilstand i hukommelsen. 15 minutter bliver til 5 (eller 0 hvis
  `control_warmup_minutes` sættes til 0, hvilket skemaet tillader).
- **Predbats plan har ingen forældelsesgrænse** — `__main__.py:544-549` bruger
  ikke `state.last_changed`, selv om `State` bærer det og COP-målingen bruger
  det omhyggeligt. En død Predbat giver gårsdagens priser, og vagten går igennem.
- **Selvopdatering kan løkke** — `bootstrap.py:72-75` og `selfupdate.py:267-270`
  sletter `.revision` ved tilbagerulning, så `__main__.py:934` henter samme
  ødelagte commit igen. Kun farlig med `auto_update: true`.
- **Hentet kode skygger for imaget** — `Dockerfile:25` sætter
  `PYTHONPATH=/data/code:/app`, og `/data` overlever butiksopdateringer. En
  rettelse udrullet gennem butikken kan blive stille ignoreret, mens loggen
  påstår den kører.
- **Korrupt `cop_table.json` overskrives tavst** — `store.py:40-44` sluger fejlen,
  `migrate.py:89` migrerer ikke igen fordi filen findes, og `_maybe_save` skriver
  den tomme tabel tilbage fem minutter senere. `store.backup()` findes, men
  kaldes ingen steder.
- **Web-serveren kan POST'es af andre containere** — `web.py:220` lytter på
  0.0.0.0. Ingress' login gælder ikke internt på Supervisor-netværket, og
  `/system` henter kode fra internettet.
- **Manglende effektføler bliver til 0 kW** — `__main__.py:557-558` (`or 0.0`)
  gør «ukendt» til «balanceret» uden en note.
- **Udetemperaturen fra Node-RED har ingen alder** — `__main__.py:111-115`. Dør
  MQTT-feedet, står den sidste temperatur i dagevis.
- **`FakeHa` kan ikke fejle** — `test_cycle.py:30-51` rejser aldrig noget, så
  1.2 og 1.4 kan ikke fanges af testene som de er.

---

## 2. COP-filteret forgifter tabellen lige nu ✱ verificeret

`addon/varmeopt/cop.py:70-76`

| delta-T | målinger | median COP | loftet | over loftet |
|---|---|---|---|---|
| ≤ 25 K | 1.556 | 4,53 | 6,5 | 0 |
| 25–40 K | 5.699 | 4,64 | 5,5 | 8 |
| **40–55 K** | **8.758** | **4,01** | **4,0** | **5.562** |
| > 55 K | 1.174 | 3,39 | 4,0 | 0 |

**63 % af målingerne i det bånd der rummer halvdelen af anlæggets drift, ligger
over loftet.** De tungeste er brugsvandet: F56/U8 (n=740), F56/U9 (n=526),
F56/U7 (n=512) — alle omkring COP 4,01–4,03 mod et loft på 4,0.

To følger:

1. **Migration og læring er uenige.** `from_raw` slap 54 celler (5.570 målinger)
   igennem som `learn` aldrig ville have skabt. Ingen får det at vide.
2. **Ensidig trunkering trækker nedad.** Kun høje målinger kasseres. For en
   celle med sand fordeling N(4,22; 0,30) og loft 4,0 konvergerer værdien til
   3,83 — **9 % for lavt** — og `count` vokser fire gange for langsomt.

Ved break-even COP 3,12 (el 2,20 kr, pille 0,706) er 9 % pessimisme direkte i
den beslutning der skal træffes.

Beskrivelsen «markant skarpere end Node-REDs flade 1,0–7,0» er **forkert i dette
bånd** — det flade loft ville have accepteret dem alle.

**Ret:** hæv loftet i 40–55 K-båndet til mindst p99 af de faktiske data, eller
byg filteret af tabellens egen fordeling frem for af faste tal.

---

## 3. Solvarmemodellen overvurderer systematisk ✱ verificeret

### 3.1 Geometrien er kun direkte stråling
`addon/varmeopt/solar.py:54-84`

`daily_incidence` integrerer cos(indfaldsvinkel) — **direkte** stråling. Solcast
melder **total** produktion. Diffus stråling går den anden vej: himmelsynsfaktoren
`(1+cos β)/2` er 0,854 for 45° mod 0,975 for PV-blandingen, så rent diffust er
forholdet **0,876** — solfangeren ser *mindre*.

| dag | modellens tal | diffus-andel | virkeligt forhold |
|---|---|---|---|
| 21. juni | 0,89 | 55 % | 0,88 |
| 21. marts | 1,37 | 65 % | 1,05 |
| 1. nov | 1,87 | 85 % | 1,03 |
| **21. dec** | **2,28** | **93 %** | **0,98** |

**Årstidssvinget findes ikke.** Det virkelige forhold er nogenlunde fladt
omkring 1,0. Faktor 2,5 er et artefakt af at gange et beam-forhold på en
total-prognose, og påstanden står i README, i commit-beskeder og i modulets
docstring.

Konkret forkert svar: decemberdag, Solcast melder 4 kWh rest, k=0,42,
`headroom` 6 kWh. Koden venter 3,82 kWh solvarme → `may_charge` 2,18 → under
minimumstrækket → **varmepumpen holdes tilbage.** Fysisk venter 1,63 kWh →
`may_charge` 4,37 → **lad op.** Forkert på præcis de kolde dage hvor
blokopladning er mest værd.

**Ret:** medregn diffus stråling. Kt kan tages som månedsklimatologi eller
udledes af Solcasts eget forhold mellem GHI og produktion.

### 3.2 Den asymmetriske læring er overflødig og skæv
`addon/varmeopt/solar.py:39-40, 252-255`

Argumentet holder kun hvis mætning er den eneste støjkilde. Solcasts
døgnprognose fejler tosidet — ±20-30 % er en normal dansk dag. Simuleret med
sand k = 0,400:

| støj | asymmetrisk (0,5/0,05) | symmetrisk (0,10) |
|---|---|---|
| ±10 % | 0,438 (**+9 %**) | — |
| ±20 % | 0,475 (**+19 %**) | 0,400 |
| ±30 % | 0,513 (**+28 %**) | 0,400 |

Og modulet har **allerede** et eksplicit mætningsfilter (`store_was_full`,
`solar.py:235`). Asymmetrien dublerer det og betaler med en bias. Docstringens
påstand om at den «gør en saturationsdetektor overflødig» er omvendt.

De to augustdage (0,397 / 0,422 / sandhed 0,428) er én realisering og kan ikke
skelne «asymmetrien retter mætningen» fra «asymmetrien skraldes op af støj».

**Ret:** symmetrisk EMA, og lad `store_was_full` gøre sit arbejde alene.

### 3.3 Mætningsdetektoren bruger det forkerte loft
`addon/varmeopt/__main__.py:689`

`above_heatpump_ceiling` er sand ved 60 °C, men solvarmen kan lade til 90.
Dermed kasseres de **solrigeste** dage som «mættede», selv om kollektoren havde
30 K tilbage. Læringsgrundlaget bliver skævt mod middelmådige dage — stik modsat
formålet. `peak_headroom_kwh` findes netop til dette.

---

## 4. Økonomi

- **Lader op ved første lejlighed, ikke den billigste** — `planner.py:194-201`
  finder den dyreste fremtidige time, men tester aldrig om *nu* er det billigste
  tidspunkt inden da. Priser 1,00 → 0,30 → 0,30 → 3,00: lader 24 kWh nu til 1,00
  i stedet for at vente 30 min på 0,30. Tab 4,20 kr på ét træk, og lageret er
  fyldt når den billige time kommer. `cheapest_window` findes, men bruges kun
  til visning. Testene rammer det ikke — alle prisrækker har den billigste time
  først.
- **`"dischrg"` indeholder `"chrg"`** ✱ verificeret — `prices.py:81`. En
  afladningshalvtime læses som opladning og prissættes til importprisen. Rammer
  også `_next_where(charging)` og `no_charge_first`. **Kontrollér Predbats
  faktiske tilstandsstrenge på anlægget.**
- **Batteriet prissættes før nettet** — `prices.py:246` før `:252`. Både
  `battery_discharging` og `importing` kan være sande samtidig: det betyder at
  inverteren er på loftet, og at ekstra forbrug derfor kommer fra nettet. Med
  12 kW inverter mod 16 kW varmepumpe er det ikke teoretisk.
- **Ingen energibetingelse på batteriprisen** — `prices.py:303`. `soc_percent`
  bruges kun i eksportgrenen. Et tomt batteri prissættes som et fuldt for alle
  fremtidige halvtimer.
- **Eksport-værdisættelsen kan gøre energien billigere** — `prices.py:280` tester
  på urabatteret pris, `:288` returnerer rabatteret. I båndet
  `snit < eksport < snit/0,90` vender grenen sit formål på hovedet: snit 1,00 og
  eksport 1,05 giver 0,945.
- **«Lades snart» overskrives af sunk cost** — `prices.py:299`. `max()` sætter den
  historiske pris som gulv og kasserer genanskaffelsesprisen, som er det
  økonomisk rigtige tal. Da Predbat vælger billige timer til ladning, er grenen i
  praksis død.
- **Slitagen mangler i kildevalget** — `planner.py:205` mod `:96-110`. Samme
  omkostning tælles i opladningen og ignoreres i kildevalget. 0,20 kr/kWh for
  dyrt, ved 16 kW er det 3,2 kr/time. **Afgør bevidst hvad tallet betyder.**
- **Ståtabet mangler helt** — `planner.py:205`. README opstiller det i ligningen;
  koden har det ikke, og der er ingen afstandsafhængighed overhovedet. Virker
  sammen med «lader op ved første lejlighed»: der lades så tidligt som muligt,
  hvilket maksimerer netop det tab der ikke regnes med.
- **`saving_kr` overdriver 2-3×** — `planner.py:230` ganger marginen på hele
  lagerpladsen, ikke på den varme der faktisk fortrænges i den dyre halvtime.
- **`pellet_kwh_price` bliver 0,0 ved fejlkonfiguration** — `options.py:211-215`.
  Nul eller negativ brændværdi gør pillevarmen gratis, tavst.

---

## 5. Modeller

- **Tabt yderføler flytter lagerenergien 50 %** — `tank.py:47-55`. Rigtigt når
  midterføleren falder ud, forkert når en yderføler gør. 500 L, 60/50/30:
  9,57 kWh bliver til 14,36 kWh (+50 %) hvis bundføleren dør. Intet degraderer
  tilliden.
- **`_blend_count` bruger `w/n` hvor variansen kræver `w²/n`** — `cop.py:108-117`.
  Et forsvindende islæt af en tynd celle halverer troværdigheden. **503 punkter
  på den rigtige tabel** hvor koden blander med fabrikskurven, men variansbaseret
  n_eff ≥ 5. Eksempel `lookup(54.3, -9)`: koden 2,79, evidensen 3,33 — omkring
  break-even 3,12 **vender beslutningen.**
- **Eksakt fremløbsrække spærrer for naboerne** — `cop.py:272-277`. Rammer `flow`
  en eksisterende række, bruges kun den, selv om naborækkerne har rigelig data
  ved samme udetemperatur. 129 sådanne punkter.
- **Interpolerede værdier vægtes ikke efter belægning** — `cop.py:301, 337`. En
  n=1-celle kan dominere fuldstændigt: `lookup(40.8, 10)` giver 4,77, mens 736
  omkringliggende målinger siger ~5,05.
- **`count` er ikke præcisionen** — `cop.py:211-213`. Cellen er en EMA med
  α ≥ 0,05, altså en hukommelse på ~39 målinger, men `count` vokser uhindret til
  7.867. Tabellen er et glidende gennemsnit over de seneste ~40, ikke over 17.187.
- **Den udledte varmekurve er ikke monoton** — `curve.py:139-162`. U9 → 38,8 mod
  U10 → 40,8: en varmere prognose giver et højere setpunkt og dermed lavere COP,
  2 K forkert vej, netop i efterårets beslutningsbånd.
- **Balancen mangler tanktab og brugsvandsudtag** — `demand.py:120-131`. 1000 L
  ved 55 °C taber 0,15-0,25 kW. Under |netto| ≈ 0,25 kW er *fortegnet* forkert.
- **`Load.kw` regner effekt uden `circulating`** — `demand.py:80-81`. En hængende
  flowmåler på 5 l/h giver 101 timers restlevetid i stedet for `None`.
- **`charge_percent` overvurderer over loftet** — `tank.py:144-147`. Tæller over
  `reference`, nævner over `ceiling`. Tank 90/70/40 giver 84,6 % mod ærlige 78 %.
- **NaN passerer `tank.layers`** — `tank.py:40, 60, 66`. Latent; `ha.py:47` lukker
  indgangen i dag. Samme i `solar.learn` (`solar.py:218-256`), hvor en NaN
  forgifter skalafaktoren til næste genstart.
- **`confidence` ser bort fra afstand** — `curve.py:129-135`. Ved −20 °C returneres
  n=70 fra U−10, selv om der ikke findes en måling inden for 10 K.
- **`WH_PER_LITER_K` — prøven beviser ikke det den siger** — `tank.py:20-23`.
  1,149 er korrekt regnet, men UVR'en bruger 1,163 (uden tæthedskorrektion), og
  de 0,01 kW forskel *er* faktoren. Prøven bekræfter at flow og ΔT læses rigtigt,
  ikke hvilken konstant der er den rigtige. 1,2 % systematisk.
- **Kurven udelukker kun ét af to faste setpunkter** — `curve.py:150`. Kører spa
  på et andet setpunkt end 56, ligger dets målinger i kurven som en falsk
  vandret linje.
- **Fortidige prognosepunkter sorteres efter temperatur** — `forecast.py:64-67`.
  Harmløst ved timeopløsning, forkert ved kvarter.

---

## Det der blev efterprøvet og fundet rigtigt

- Pillefyr-loftet i `cheapest_heat` er korrekt anvendt — kun på fremtidssiden,
  hvilket er den rigtige asymmetri
- Negative priser flyder korrekt igennem hele kæden, ingen `abs()` klipper fortegn
- Enheder er konsistente mellem sol, lager og balance
- `nodered_lookup` er en fair modpart, ingen stråmand
- Solgeometriens **formler** er matematisk eksakte (afvigelse 4×10⁻¹⁶ mod
  uafhængig udledning) — fejlen er hvad de anvendes på, ikke hvordan de regnes
- `cheapest_window`s deadline-semantik, ingen off-by-one
- Stifiltreringen i `selfupdate` holder — ingen path traversal
- Ingen division med nul fundet nogen steder
- `compare.py` regner i konsistente enheder; «på spil» er ærligt navngivet
