# Porting ATA4 → ATA5 — Roadmap operativa

## 1. Cosa è cambiato rispetto ad ATA4

| Ambito | ATA4 | ATA5 |
|---|---|---|
| Ente | ATA4 Fermo | ATA5 Ascoli Piceno |
| Comuni | ~40 | **33** (vedi `data/credentials.example.json`) |
| Dominio dataroom | `drive.provincia.fm.it` (o simile) | `drive.atarifiuti.ap.it` |
| Struttura dataroom | Link → cartella MTR3 2026 diretta, un solo "Scarica tutto" alla radice | Link → **indice di tutte le annualità**; lo scanner deve entrare in **3 sotto-cartelle** e fare "Scarica tutto" in ognuna |
| Cartelle target | — | `Pef Validato 2026`, `Gestore 2026`, `Comune 2026` |

Tutto il resto (classificazione documenti, storicizzazione, dashboard, workflow, cruscotto HTML) è **invariato**: nessuna funzionalità di ATA4 è stata rimossa, è stata solo adattata la fase di download.

## 2. Modifiche al codice (già applicate in questa consegna)

### `scripts/scanner.py`
- Aggiunta costante `TARGET_FOLDERS` con label, pattern regex e virtual root delle 3 sotto-cartelle.
- `download_zip(...)` → **`download_zips(...)`**: dopo il login entra in ciascuna sotto-cartella target, clicca "Scarica tutto", attende il download. Ritorna la lista `[(virtual_root, zip_path), ...]`. Se nessuna delle 3 è trovata → `EMPTY_DATAROOM`.
- Helper nuovi: `_find_and_click_folder`, `_click_scarica_tutto`, `_wait_download_complete`.
- `analyze_zip(...)` → **`analyze_zips(...)`**: itera su più zip e prefissa ogni entry con `virtual_root` (`Gestore 2026/...`, `Comune 2026/...`, `PEF Validato 2026/...`) così la funzione `classify_file` esistente continua a funzionare senza modifiche grazie alle regex di root già presenti (`\bgestore\b`, `\bcomune\b`, `pef.*validat`).
- `main()`: loop aggiornato per gestire la lista di zip (retry, pulizia file, hash combinato).
- Dominio in `--unsafely-treat-insecure-origin-as-secure` aggiornato a `drive.atarifiuti.ap.it`.
- Prefisso directory temp `ata5_`, log e messaggi rinominati.

### `.github/workflows/scan.yml`
- Nome workflow → "Scansione Dataroom ATA5".
- `concurrency.group` → `scan-dataroom-ata5`.
- `user.name` git → `ATA5 Scanner Bot`.

### `data/credentials.example.json`
- Pre-popolato con i 33 comuni estratti dal foglio `Password_Comuni.xlsx`.
- Schema identico ad ATA4: `id`, `comune`, `gestore`, `url`, `pwd`, `advisor`.
- **Da NON committare in chiaro**: usare il segreto `CREDENTIALS_JSON` come in ATA4.

### `docs/index.html` (cruscotto)
Il cruscotto di ATA4 va copiato e rebrand-ato. Non è stato riscritto per non perdere nulla: è sufficiente un find-and-replace globale sul file di ATA4:

```
ATA4  → ATA5
ata4  → ata5
Pdoor/ata4-mtr3  → Pdoor/ata5-mtr3
pdoor.github.io/ata4-mtr3  → pdoor.github.io/ata5-mtr3
ATA4 — Cruscotto MTR3 2026-2027 → ATA5 — Cruscotto MTR3 2026
ATA4_MTR3_2026_stato.csv → ATA5_MTR3_2026_stato.csv
LS_NOTES = "ata4_notes_v1" → "ata5_notes_v1"
LS_SCADENZE = "ata4_scadenze_v1" → "ata5_scadenze_v1"
LS_OVERRIDES = "ata4_overrides_v1" → "ata5_overrides_v1"
LS_GH_TOKEN = "ata4_gh_token_v1" → "ata5_gh_token_v1"
LS_SESSION = "ata4_session_v1" → "ata5_session_v1"
```

Nessuna logica React/JS del cruscotto viene toccata: la struttura dati di `dashboard.json` è identica.

### `docs/data/dashboard.json` e `overrides.json`
File di bootstrap iniziali (dashboard vuota, overrides vuoto) — vengono rigenerati dallo scanner al primo run.

## 3. Roadmap passo-passo per rendere ATA5 operativo su GitHub

1. **Clonare il repo pubblico**
   ```
   git clone https://github.com/Pdoor/ata5-mtr3.git
   cd ata5-mtr3
   ```

2. **Copiare la consegna** (questa cartella `ata5-mtr3/`) dentro la working copy mantenendo la struttura:
   ```
   scripts/scanner.py
   .github/workflows/scan.yml
   data/credentials.example.json
   docs/data/dashboard.json
   docs/data/overrides.json
   docs/.nojekyll
   .gitignore
   README.md
   ROADMAP.md
   ```

3. **Portare il cruscotto HTML**: copiare `docs/index.html` da ATA4 e applicare la find-and-replace elencata al punto 2. Commit a parte per tracciabilità.

4. **Verificare credenziali**: aprire `data/credentials.example.json`, controllare che i 33 comuni abbiano `url`/`pwd` corretti. Eventuali comuni con password nuova/ruotata vanno aggiornati qui.

5. **Generare il segreto GitHub**
   ```
   base64 -w0 data/credentials.example.json > /tmp/cred.b64
   ```
   Copiare il contenuto di `/tmp/cred.b64` e impostarlo come repository secret `CREDENTIALS_JSON` su `Settings → Secrets and variables → Actions → New repository secret`.

6. **Abilitare GitHub Pages** su `Settings → Pages`: source `Deploy from a branch`, branch `main`, folder `/docs`. L'URL pubblico del cruscotto sarà `https://pdoor.github.io/ata5-mtr3/`.

7. **Abilitare i workflow** in `Settings → Actions → General → Allow all actions` e dare i permessi di write a `Workflow permissions → Read and write`.

8. **Primo run manuale**: tab `Actions → Scansione Dataroom ATA5 → Run workflow` (filtro vuoto). Verificare i log: ogni comune deve loggare l'ingresso nelle 3 sotto-cartelle e il click "Scarica tutto".

9. **Run di prova su un singolo comune** prima del run completo: `Run workflow → filter = "Ascoli Piceno"` per validare la logica di navigazione senza stressare le dataroom.

10. **Se la navigazione delle sotto-cartelle fallisce**: catturare l'HTML della pagina indice (basta aggiungere temporaneamente `driver.save_screenshot(...)` e `open("page.html","w").write(driver.page_source)`) e aggiornare i selettori in `_find_and_click_folder` / i pattern in `TARGET_FOLDERS`. Il resto del flow non richiede modifiche.

11. **Commit finale, merge su main, attesa dello schedule orario**. Il dashboard si popolerà progressivamente (commit ogni 5 comuni come in ATA4).

12. **Rebranding front-end tardivo**: dopo il primo run OK, aggiungere il logo ATA5 / eventuale favicon nel `docs/`.

## 4. Vincoli rispettati

- Tutte le funzionalità ATA4 (storicizzazione upload, rilevazione sostituzioni, stato `missing/received/removed`, processabilità SI/SI_RISERVA/NO, merge semantico su git, commit incrementali, retry, dataroom vuota, note manuali, overrides online, scadenze locali/online, export CSV) sono **integralmente preservate**.
- L'unica differenza è la fase di download multi-cartella.
- La struttura `dashboard.json` è invariata, quindi il cruscotto ATA4 funziona senza modifiche di logica (solo rebranding testuale).
