# Daglig uppdatering av "Sveriges kandidater"

Filer:

- `sveriges-kandidater.html` – **din sajt, med en bugg åtgärdad** (se nedan). Använd
  den här filen, inte din ursprungliga.
- `build_kandidater.py` – laddar ner `kandidaturer.csv` från val.se och bygger om
  data-blocket i HTML-filen.
- `.github/workflows/uppdatera-kandidater.yml` – kör skriptet en gång per dygn på
  GitHub och committar den uppdaterade HTML-filen.
- denna fil.

## Buggen som åtgärdats

I kartans tooltip användes variabeln `curParty` utan att den någonsin deklarerades.
Det gör att varje musrörelse över kartan kastar ett `ReferenceError` och tooltipen
slutar fungera. (Felet syns inte vid en vanlig syntaxkontroll eftersom det inträffar
först när koden körs.) Fixen är en rad: `curParty` deklareras som tom sträng, vilket
ger oförändrat utseende men ingen krasch. Allt annat i filen är oförändrat.

Eftersom byggskriptet bara skriver om data-blocket (`<script id="DATA">`) och inte
rör koden, lever fixen kvar även efter de dagliga uppdateringarna.

## Så kommer det igång

1. Lägg dessa i ett GitHub-repo (Public för gratis Pages), i repots rot:

       sveriges-kandidater.html
       build_kandidater.py
       .github/workflows/uppdatera-kandidater.yml

   Workflow-filen läggs lättast via **Add file -> Create new file** och sökvägen
   `.github/workflows/uppdatera-kandidater.yml`.
2. **Settings -> Actions -> General -> Workflow permissions**: *Read and write permissions*.
3. **Settings -> Pages**: *Deploy from a branch*, branch `main`, mapp `/ (root)`.
   - Sajten nås på `…github.io/<repo>/sveriges-kandidater.html`.
   - Vill du ha den på rot-URL:en: döp om filen till `index.html` och byt `HTML_FILE`
     överst i skriptet samt filnamnet i workflow-filen.
4. Testa: **Actions -> Daglig uppdatering -> Run workflow**. Titta på steget
   "Bygg om data" för siffrorna från `--self-check`.

## Köra lokalt

    python build_kandidater.py --self-check          # laddar ner och bygger om
    python build_kandidater.py --csv-file fil.csv    # bygg från en redan nedladdad CSV

## Vad som uppdateras - och vad som inte gör det

Allt som går att räkna fram ur `kandidaturer.csv` byggs om: antal kandidater och
kandidaturer, kön, ålder, partier, yrken, län- och kommunfördelning, kartan
(inkl. kön/ålder/65+ per kommun), vallistorna och namnsöket.

Det som bygger på **2022 års mandat** och inte finns i CSV:n:
- `kommunValbar` (kartans ruta "På valbar plats 2022") - bevaras oförändrad.
- `N` = "valbar gräns (mandat 2022)" i varje vallista - **bevaras** genom att matcha
  nya listor mot de gamla på (valtyp, valkretsnamn, parti, valkretsbeteckning).
  Hittas ingen match sätts `N = -1` och då visas ingen valbar-gräns för den listan.
- Flaggan "valbar plats" i namnsöket (`srec[3]`) - **härleds** ur listorna + `N`
  (placering <= N => valbar). Saknas `N` blir flaggan 0/-1, dvs. taggen visas inte.

`self-check` skriver bl.a. ut hur många `N`-värden som bevarades (av ca 2102). Är den
siffran låg har val.se troligen ändrat list-/områdesnamn; hör av dig så justerar vi
matchningen.

`GEO`-blocket (kartans geografi) rörs aldrig.

## Antaganden (ändras överst i skriptet)

- Endast rader med `GILTIG = J`. En person = ett `KANDIDATNUMMER` (valtyp slås ihop).
- Partier med >= 100 personer listas separat, övriga som "Övriga".
- Län per person härleds ur folkbokföringskommun via `GEO`.
- Yrke = texten efter "ålder, " i `VALSEDELSUPPGIFT`, gemener.

## Ärlig brasklapp om validering

Skriptets logik är testad mot exempelrader och mot filens verkliga `GEO`/`lists`-data
(kodningen stämmer exakt - t.ex. hamnar Göteborg på samma index som i originalet).
Men hela pipelinen mot den skarpa, fullstora CSV:n kan först köras på GitHub - den
här miljön når inte val.se. Kör därför `--self-check` efter första körningen och
kontrollera att `n`, `cand`, `ageMean` ser rimliga ut och att en hög andel `N`-värden
bevarades.
