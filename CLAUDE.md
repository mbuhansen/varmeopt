# Retningslinjer for varmeopt

## Sproget er dansk med æ, ø og å

Det gælder **alt**: kommentarer, docstrings, strengliteraler, log-linjer,
attributnavne på entiteter, README og commit-beskeder.

Skriv `måling`, ikke `maaling`. `først`, ikke `foerst`. `på`, ikke `paa`.

Filerne er UTF-8, og bogstaverne virker overalt hvor de ender: i Home
Assistants log, i entiteternes tilstande og attributter, på add-on'ens
web-side og i git. Der har aldrig været en teknisk grund til at
translitterere — det blev efterprøvet den 11. september 2026 mod den
kørende log fra anlægget, hvor `står` og `slået` stod korrekt tusinder af
gange.

### Undtagelserne

Nogle ord har et ægte `oe` eller `aa`, som ikke er en erstatning:

* `koefficient`, `tabskoefficient` — sådan staves de.
* `Segoe UI` — skrifttypenavnet i web-sidens CSS.
* Bøjning af en vokalstamme: `spa` + `en` = `spaen`, `tro` + `et` = `troet`,
  `nabo` + `er` = `naboer`, `skema` + `et` = `skemaet`. Her møder to
  bogstaver hinanden over en morfemgrænse.

Et blindt `s/aa/å/` ødelægger alle fem. Skal der konverteres i større
omfang, så byg ordlisten ud af `tokenize`-tokens af typen `COMMENT`,
`STRING` og `FSTRING_MIDDLE` — aldrig `NAME` — og **læs listen igennem**,
før noget skrives.

### Hvorfor det står her

Fordi det gled. Projektet startede med korrekt æøå og endte på 65–100 %
translitteration i nye commits i løbet af ti dage. Ingen besluttede det:
hver ny session så translittereret tekst i linjen ovenover og skrev videre
i samme stil, og commit-emnerne fodrede driften.

**Ser du `maaling` i en fil du redigerer: ret den.** Skriv ikke videre i
den stil — det var præcis sådan det gled sidst.

## Tests

```bash
cd addon
python -B -m unittest discover -s tests -t .
```

Brug `unittest`, ikke `pytest`. Den globale fortolker har
`pytest-homeassistant-custom-component` installeret, som tvinger
`pytest-socket`-blokering på ethvert `pytest`-kald og giver snesevis af
fejl i *setup* — ikke én rigtig testfejl. `unittest` omgår det uden noget
venv.

En grøn suite er ikke i sig selv et bevis. Flere af de fejl gennemgangene
har fundet, lå i kode der havde grønne tests: testene kørte én cyklus fra
en frisk tilstand, hvor det der skulle skilles ad, altid var ens. Skriver
du en test til en rettelse, så **efterprøv at den bliver rød uden
rettelsen**.

## Udrulning

Home Assistant installerer add-on'en fra `master` på det offentlige repo,
så et push er en udrulning. Butikken tilbyder kun en opdatering når
`version:` i `addon/config.yaml` ændrer sig — pusher du en rettelse uden at
bumpe den, sker der intet på boksen, og det ligner at det virkede.
