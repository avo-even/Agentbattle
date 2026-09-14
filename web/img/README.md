# Persona-bilder for Gatekeeper

Legg de tre portrettene her med nøyaktig disse filnavnene:

| fil | hvem | hvelv |
|---|---|---|
| `bjorn.jpg` | Bjørn (mørkt hår, lys skjorte) | 1, lettest |
| `pia.png` | Pia (blondt hår, svart genser) | 2 |
| `arne-benjamin.png` | Arne Benjamin (grå genser) | 3, final boss |

Formatet spiller ingen rolle (jpg, png, webp), bare filnavnet i `config.yaml`
matcher fila her. Kvadratiske bilder blir beskåret til sirkler på prosjektoren.
Mangler en fil, vises initialene i stedet, så ingenting knekker.

Filnavnene er konfigurert i `config.yaml` under `gatekeeper.tiers[].image`.
Bildene serveres fra `/img/` og blir med i Docker-imaget ved neste redeploy.
