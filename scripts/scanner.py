# -*- coding: utf-8 -*-
"""
Scanner Dataroom ATA5 MTR3 2026
Gira su GitHub Actions, analizza ogni dataroom, classifica i documenti,
traccia la storia dei caricamenti, e produce dashboard.json.

DIFFERENZA rispetto ATA4
────────────────────────
Le dataroom ATA5 non puntano direttamente alla cartella MTR3 2026: dopo
login (password) mostrano l'elenco di TUTTE le annualità. Lo scanner deve
quindi entrare nelle sottocartelle specifiche dell'annualità MTR3 2026 e
scaricare ciascuna con il pulsante "Scarica tutto":

    • Pef Validato 2026
    • Gestore 2026
    • Comune 2026

Gli zip scaricati vengono poi fusi in memoria e analizzati con la stessa
logica di classificazione di ATA4 (basata sulla cartella radice).
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import zipfile
import tempfile
from datetime import datetime, timezone
from io import BytesIO

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

def log_debug(msg):
    """Funzione per stampare log immediati su GitHub Actions"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}")
    sys.stdout.flush()

# ═══════════════════════════════════════════════════════════
#  CONFIGURAZIONE SCADENZE
# ═══════════════════════════════════════════════════════════
SCADENZE = {
    "termine_arera_mtr3": "2026-07-31",    # Scadenza circolare ARERA MTR3
    "termine_ata_gestori": "2026-05-31",   # Scadenza ATA per i gestori
    "termine_ata_comuni": "2026-05-31",    # Scadenza ATA per i comuni
}

# ═══════════════════════════════════════════════════════════
#  CARTELLE TARGET ATA5 (annualità MTR3 2026)
# ═══════════════════════════════════════════════════════════
# L'ordine è quello in cui verranno aperte e scaricate.
# I pattern regex vengono usati per individuare i link/elemento
# di ciascuna cartella nella pagina post-login della dataroom.
TARGET_FOLDERS = [
    {
        "label": "Pef Validato 2026",
        "patterns": [r"pef\s*validat\w*\s*2026", r"pef.*valid.*2026"],
        "virtual_root": "PEF Validato 2026",  # prefisso usato in classify
    },
    {
        "label": "Gestore 2026",
        "patterns": [r"gestor\w*\s*2026", r"operator\w*\s*2026"],
        "virtual_root": "Gestore 2026",
    },
    {
        "label": "Comune 2026",
        "patterns": [r"comun\w*\s*2026"],
        "virtual_root": "Comune 2026",
    },
]

# ═══════════════════════════════════════════════════════════
#  CLASSIFICAZIONE DOCUMENTI
# ═══════════════════════════════════════════════════════════
# Pattern regex per classificare i file trovati nello zip.
# Ogni file viene confrontato con questi pattern (case-insensitive).
# Il primo match vince. "source" indica se è del gestore o del comune.

DOC_PATTERNS = [
    # GESTORE
    {"key": "tool_mtr3",       "source": "gestore", "patterns": [
        r"tool.*mtr", r"appendice.*1.*gest", r"app.*1.*gest",
        r"pef.*grezzo.*gest", r"mtr3?.*gest.*tool", r"gest.*tool",
        r"gest.*appendice.*1", r"gest.*app.*1",
    ]},
    {"key": "relazione",       "source": "gestore", "patterns": [
        r"relazione.*gest", r"gest.*relazione", r"appendice.*2.*gest",
        r"app.*2.*gest", r"gest.*appendice.*2", r"gest.*app.*2",
    ]},
    {"key": "dich_veridicita", "source": "gestore", "patterns": [
        r"dich.*veridic.*gest", r"gest.*dich.*veridic",
        r"appendice.*3.*gest", r"app.*3.*gest",
        r"gest.*appendice.*3", r"gest.*app.*3", r"veridicit.*gest",
    ]},
    {"key": "altre_com",       "source": "gestore", "patterns": [
        r"comunicazion.*gest", r"gest.*comunicazion", r"gest.*altr",
        r"format.*gest", r"gest.*format", r"dati.*gest",
    ]},
    # COMUNE
    {"key": "tool_mtr3_c",       "source": "comune", "patterns": [
        r"tool.*mtr.*comun", r"comun.*tool", r"appendice.*1.*comun",
        r"app.*1.*comun", r"comun.*appendice.*1", r"comun.*app.*1",
        r"pef.*grezzo.*comun", r"mtr3?.*comun.*tool",
    ]},
    {"key": "relazione_c",       "source": "comune", "patterns": [
        r"relazione.*comun", r"comun.*relazione", r"appendice.*2.*comun",
        r"app.*2.*comun", r"comun.*appendice.*2", r"comun.*app.*2",
    ]},
    {"key": "dich_veridicita_c", "source": "comune", "patterns": [
        r"dich.*veridic.*comun", r"comun.*dich.*veridic",
        r"appendice.*3.*comun", r"app.*3.*comun",
        r"comun.*appendice.*3", r"comun.*app.*3", r"veridicit.*comun",
    ]},
    {"key": "altre_com_c",       "source": "comune", "patterns": [
        r"comunicazion.*comun", r"comun.*comunicazion", r"comun.*altr",
        r"format.*comun", r"comun.*format", r"dati.*comun",
    ]},
]

# Fallback: se il file non matcha nessun pattern specifico,
# prova a capire almeno se è gestore o comune dalla struttura cartelle
FOLDER_HINTS = {
    "gestore": [r"gest", r"gestore", r"operatore"],
    "comune":  [r"comun", r"ente", r"municipio"],
}


def classify_file(filepath):
    """
    Classifica un file in base SOLO alla struttura delle cartelle dello zip.

    Struttura attesa (dopo merge dei 3 zip ATA5 con prefisso virtuale):
      {Fonte}/{Allegato N - descrizione}/{filename}
      Fonte    : "Comune 2026", "Gestore 2026", "PEF Validato 2026"
      Allegato : 1=Tool, 2=Relazione, 3=Dich.Veridicità, 4=Altre Comunicazioni

    NESSUN fallback regex su nomi file — la classificazione è SOLO per cartelle.
    """
    name_lower = filepath.lower().replace("\\", "/")
    basename = os.path.basename(name_lower)

    # Ignora file di sistema, thumbs, desktop.ini, ecc.
    if basename.startswith(".") or basename in ("thumbs.db", "desktop.ini", ".ds_store"):
        return None, None

    parts = name_lower.split("/")
    if not parts:
        return None, "sconosciuto"

    root = parts[0].strip()

    # ── PEF Validato 2026 ──
    if re.search(r'pef.*validat|validat.*pef', root):
        return "pef_validato", "ata5"

    # ── Comune / Gestore con sottocartelle Allegato ──
    if len(parts) >= 2:
        subfolder = parts[1].strip()

        # Fonte dalla cartella radice
        if re.search(r'\bcomune\b', root):
            source = "comune"
        elif re.search(r'\b(gestore|operatore)\b', root):
            source = "gestore"
        else:
            source = None

        if source:
            # Normalizza "Allegato2" → "Allegato 2" per uniformità
            subfolder_norm = re.sub(r'allegato(\d)', r'allegato \1', subfolder)

            if   re.search(r'allegato\s*1\b', subfolder_norm): allegato = 1
            elif re.search(r'allegato\s*2\b', subfolder_norm): allegato = 2
            elif re.search(r'allegato\s*3\b', subfolder_norm): allegato = 3
            elif re.search(r'allegato\s*4\b', subfolder_norm): allegato = 4
            else:                                               allegato = None

            if allegato is not None:
                key_map = {
                    ("comune",  1): "tool_mtr3_c",
                    ("comune",  2): "relazione_c",
                    ("comune",  3): "dich_veridicita_c",
                    ("comune",  4): "altre_com_c",
                    ("gestore", 1): "tool_mtr3",
                    ("gestore", 2): "relazione",
                    ("gestore", 3): "dich_veridicita",
                    ("gestore", 4): "altre_com",
                }
                doc_key = key_map.get((source, allegato))
                if doc_key:
                    return doc_key, source

    # Nessun fallback regex — solo struttura cartelle
    source_guess = "sconosciuto"
    for src, hints in FOLDER_HINTS.items():
        for hint in hints:
            if re.search(hint, name_lower):
                source_guess = src
                break

    return None, source_guess


def file_hash(data):
    """SHA-256 di bytes."""
    return hashlib.sha256(data).hexdigest()


# ═══════════════════════════════════════════════════════════
#  SELENIUM DOWNLOAD
# ═══════════════════════════════════════════════════════════

def create_driver(download_dir):
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--allow-running-insecure-content")
    opts.add_argument("--unsafely-treat-insecure-origin-as-secure=http://drive.atarifiuti.ap.it")
    opts.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    })
    return webdriver.Chrome(options=opts)


MAX_RETRIES = 2          # Tentativi extra per ogni comune fallito
PAGE_LOAD_TIMEOUT = 40   # Timeout caricamento pagina (secondi)


EMPTY_MARKERS = ("Nessun files in questa pagina", "No files in this page")


def _page_is_empty(driver):
    """True se la pagina mostra l'avviso 'Nessun files in questa pagina'."""
    if driver is None:
        return False
    try:
        src = driver.page_source
        return any(m in src for m in EMPTY_MARKERS)
    except Exception:
        return False


def _wait_download_complete(download_dir, timeout=120, ignore=None, driver=None):
    """
    Aspetta che compaia un nuovo file .zip nella directory di download.
    Se `driver` è fornito, interrompe subito l'attesa quando la pagina
    mostra 'Nessun files in questa pagina' (cartella vuota, nessun zip in arrivo),
    restituendo la sentinella "EMPTY".
    """
    ignore = ignore or set()
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(2)
        files = set(os.listdir(download_dir))
        crdowns = [f for f in files if f.endswith(".crdownload")]
        zips = [f for f in files - ignore if f.endswith(".zip")]
        if not crdowns and zips:
            time.sleep(1)
            return os.path.join(download_dir, zips[0])
        # Short-circuit: se non ci sono download in corso e la pagina
        # dichiara esplicitamente che non ci sono file, esci subito.
        if not crdowns and _page_is_empty(driver):
            return "EMPTY"
    return None


def _find_and_click_folder(driver, patterns):
    """
    Cerca un link/elemento cliccabile la cui etichetta matcha uno dei
    pattern (case-insensitive) e ci clicca sopra. Restituisce True/False.
    """
    candidates = []
    try:
        candidates += driver.find_elements(By.XPATH, "//a")
        candidates += driver.find_elements(By.XPATH, "//*[@role='link']")
        candidates += driver.find_elements(By.XPATH, "//div[contains(@class,'folder') or contains(@class,'cartel')]")
        candidates += driver.find_elements(By.XPATH, "//li//*[self::a or self::span or self::div]")
    except WebDriverException:
        pass

    for el in candidates:
        try:
            txt = (el.text or "").strip().lower()
            if not txt:
                continue
            for p in patterns:
                if re.search(p, txt, re.IGNORECASE):
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    except Exception:
                        pass
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                    return True
        except Exception:
            continue
    return False


def _click_scarica_tutto(driver, timeout=15):
    """Clicca sul pulsante 'Scarica tutto' / 'Download all'. True se cliccato."""
    for label in ["Scarica tutto", "SCARICA TUTTO", "Scarica Tutto", "Download all"]:
        try:
            btn = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, f"//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{label.lower()}')] | //a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{label.lower()}')]"))
            )
            btn.click()
            log_debug(f"    Click '{label}'")
            return True
        except TimeoutException:
            continue
    return False


def download_zips(url, password, download_dir, timeout=120):
    """
    Scarica le sottocartelle ATA5 (Pef Validato 2026, Gestore 2026, Comune 2026).

    Restituisce:
      - "EMPTY_DATAROOM" se la pagina post-login è vuota o nessuna delle
        cartelle target è presente
      - None in caso di errore
      - lista di tuple (virtual_root, zip_path) altrimenti
    """
    # Pulisci directory
    for f in os.listdir(download_dir):
        fp = os.path.join(download_dir, f)
        if os.path.isfile(fp):
            os.unlink(fp)

    driver = create_driver(download_dir)
    try:
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        log_debug(f"Navigazione: {url}")
        try:
            driver.get(url)
        except TimeoutException:
            log_debug(f"ERRORE: Timeout caricamento pagina ({PAGE_LOAD_TIMEOUT}s)")
            return None
        time.sleep(3)

        # Password
        try:
            pwd_input = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="password"]'))
            )
            pwd_input.send_keys(password + Keys.ENTER)
            log_debug("Password inserita")
        except TimeoutException:
            log_debug("Campo password non trovato")

        time.sleep(3)

        # Pagina vuota?
        try:
            src = driver.page_source
            if "Nessun files in questa pagina" in src or "No files in this page" in src:
                log_debug("Dataroom vuota al livello radice")
                return "EMPTY_DATAROOM"
        except Exception:
            pass

        # Memorizzo l'URL della pagina "indice annualità" per tornarci
        index_url = driver.current_url

        downloaded = []
        already_present = set(os.listdir(download_dir))

        for target in TARGET_FOLDERS:
            label = target["label"]
            log_debug(f"  → Cartella target: {label}")

            # Torna alla pagina indice (dalla seconda iterazione in poi)
            if driver.current_url != index_url:
                try:
                    driver.get(index_url)
                    time.sleep(2)
                except Exception:
                    pass

            # 1) clicca sulla sottocartella
            if not _find_and_click_folder(driver, target["patterns"]):
                log_debug(f"    AVVISO: cartella '{label}' non trovata, skip")
                continue
            time.sleep(3)

            # 2) se la sottocartella è vuota, skip
            if _page_is_empty(driver):
                log_debug(f"    Cartella '{label}' vuota (nessun file)")
                continue

            # 3) clicca "Scarica tutto"
            if not _click_scarica_tutto(driver):
                log_debug(f"    ERRORE: pulsante Scarica tutto non trovato in '{label}'")
                continue

            # 4) aspetta completamento download (short-circuit se la pagina
            #    mostra 'Nessun files in questa pagina' dopo il click)
            zpath = _wait_download_complete(
                download_dir, timeout=timeout, ignore=already_present, driver=driver
            )
            if zpath == "EMPTY":
                log_debug(f"    Cartella '{label}' vuota dopo click (skip senza attendere timeout)")
                continue
            if not zpath:
                log_debug(f"    ERRORE: timeout download '{label}'")
                continue

            log_debug(f"    Scaricato: {os.path.basename(zpath)}")
            downloaded.append((target["virtual_root"], zpath))
            already_present = set(os.listdir(download_dir))

        if not downloaded:
            # Nessuna delle 3 cartelle target presenti → dataroom vuota per MTR3 2026
            return "EMPTY_DATAROOM"

        return downloaded

    except WebDriverException as e:
        log_debug(f"ERRORE Selenium: {e}")
        return None
    finally:
        driver.quit()


# ═══════════════════════════════════════════════════════════
#  ANALISI ZIP
# ═══════════════════════════════════════════════════════════

def analyze_zips(zip_entries):
    """
    Analizza più zip (lista di (virtual_root, zip_path)) e li fonde in
    un unico elenco di file, prefissando il path con virtual_root in modo
    che classify_file veda una struttura del tipo "Gestore 2026/Allegato 1/...".

    Restituisce:
      - combined_hash: hash composto da tutti gli zip
      - files: lista unificata di {name, path, hash, size, doc_key, source, classified}
    """
    h = hashlib.sha256()
    files = []

    for virtual_root, zip_path in zip_entries:
        with open(zip_path, "rb") as fh:
            h.update(fh.read())

        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                data = zf.read(info.filename)
                fhash = file_hash(data)

                # Prefissa con virtual_root solo se lo zip non lo contiene già
                inner = info.filename.replace("\\", "/").lstrip("/")
                first = inner.split("/")[0].lower()
                if first != virtual_root.lower():
                    merged_path = f"{virtual_root}/{inner}"
                else:
                    merged_path = inner

                doc_key, source = classify_file(merged_path)

                files.append({
                    "name": os.path.basename(inner),
                    "path": merged_path,
                    "hash": fhash,
                    "size": info.file_size,
                    "doc_key": doc_key,
                    "source": source,
                    "classified": doc_key is not None,
                })

    return h.hexdigest(), files


# ═══════════════════════════════════════════════════════════
#  AGGIORNAMENTO STATO CON STORICIZZAZIONE
# ═══════════════════════════════════════════════════════════

def update_comune_state(old_state, zip_hash, files, scan_time):
    """
    Aggiorna lo stato di un comune confrontando con lo stato precedente.
    Gestisce:
    - Primo caricamento di un documento
    - Sostituzione (nuovo hash per stesso doc_key)
    - Rimozione (doc_key presente prima ma non ora)
    - File multipli per stesso doc_key
    - Storicizzazione date
    """
    state = old_state.copy() if old_state else {}

    # Fingerprint sul contenuto reale dei file, indipendente dai metadati
    # dello zip (timestamp, ecc.) che cambiano ad ogni generazione dinamica.
    sorted_file_hashes = sorted((f["name"], f["hash"]) for f in files)
    content_fp = hashlib.sha256(json.dumps(sorted_file_hashes).encode()).hexdigest()
    old_fp = state.get("content_fingerprint", "")
    old_zip_hash = old_state.get("zip_hash") if old_state else None
    state["zip_hash"] = zip_hash                  # conservato per storico/debug
    state["content_fingerprint"] = content_fp
    state["last_scan"] = scan_time
    state["zip_changed"] = (content_fp != old_fp)

    if "docs" not in state:
        state["docs"] = {}
    if "all_files" not in state:
        state["all_files"] = []
    if "scan_history" not in state:
        state["scan_history"] = []

    # Registro scan
    state["scan_history"].append({
        "time": scan_time,
        "zip_hash": zip_hash,
        "changed": state["zip_changed"],
        "file_count": len(files),
    })
    # Tieni solo ultime 100 scansioni
    state["scan_history"] = state["scan_history"][-100:]

    if not state["zip_changed"] and old_zip_hash is not None:
        # Nessun cambiamento, non aggiornare i dettagli
        return state

    # ── Aggiorna documenti classificati ──
    old_docs = state.get("docs", {})
    new_docs = {}

    # Mappa doc_key -> lista file trovati in questo scan
    classified_now = {}
    for f in files:
        if f["doc_key"]:
            classified_now.setdefault(f["doc_key"], []).append(f)

    # Per ogni tipo di documento possibile
    all_doc_keys = [d["key"] for d in DOC_PATTERNS] + ["pef_validato"]
    for dk in all_doc_keys:
        old_doc = old_docs.get(dk, {})
        found_files = classified_now.get(dk, [])

        if found_files:
            # Prendi il file principale (il più grande, tipicamente il documento vero)
            main_file = max(found_files, key=lambda x: x["size"])
            old_hash = old_doc.get("current_hash")
            new_hash = main_file["hash"]

            doc = {
                "status": "received",
                "current_hash": new_hash,
                "current_file": main_file["name"],
                "current_size": main_file["size"],
                "file_count": len(found_files),
                "all_files": [{"name": f["name"], "hash": f["hash"], "size": f["size"]} for f in found_files],
            }

            if not old_doc or old_doc.get("status") == "missing":
                # Primo caricamento
                doc["first_upload"] = scan_time
                doc["last_upload"] = scan_time
                doc["replaced"] = False
                doc["upload_history"] = [{"time": scan_time, "hash": new_hash, "file": main_file["name"], "event": "primo_caricamento"}]
            elif new_hash != old_hash:
                # Sostituzione
                doc["first_upload"] = old_doc.get("first_upload", scan_time)
                doc["last_upload"] = scan_time
                doc["replaced"] = True
                history = old_doc.get("upload_history", [])
                history.append({"time": scan_time, "hash": new_hash, "file": main_file["name"], "event": "sostituzione"})
                doc["upload_history"] = history[-50:]
            else:
                # Invariato
                doc["first_upload"] = old_doc.get("first_upload", scan_time)
                doc["last_upload"] = old_doc.get("last_upload", scan_time)
                doc["replaced"] = old_doc.get("replaced", False)
                doc["upload_history"] = old_doc.get("upload_history", [])

            new_docs[dk] = doc
        else:
            # Documento non trovato in questo scan
            if old_doc and old_doc.get("status") == "received":
                # Era presente prima, ora rimosso
                doc = old_doc.copy()
                doc["status"] = "removed"
                doc["removed_at"] = scan_time
                history = doc.get("upload_history", [])
                history.append({"time": scan_time, "hash": None, "file": None, "event": "rimosso"})
                doc["upload_history"] = history[-50:]
                new_docs[dk] = doc
            else:
                new_docs[dk] = {"status": "missing"}

    state["docs"] = new_docs

    # ── File non classificati ──
    state["unclassified_files"] = [
        {"name": f["name"], "path": f["path"], "source": f["source"], "size": f["size"]}
        for f in files if not f["classified"]
    ]

    # ── Lista completa file ──
    state["all_files"] = [
        {"name": f["name"], "path": f["path"], "hash": f["hash"],
         "size": f["size"], "doc_key": f["doc_key"], "source": f["source"]}
        for f in files
    ]

    return state


# ═══════════════════════════════════════════════════════════
#  CALCOLO PROCESSABILITÀ
# ═══════════════════════════════════════════════════════════

def compute_processabilita(docs):
    """
    Determina la processabilità in base ai documenti presenti.
    - SI: tutti i documenti obbligatori ricevuti (tool + relazione + dich per entrambi)
    - SI_RISERVA: tool + relazione ok ma manca dichiarazione veridicità
    - NO: manca tool o relazione
    """
    obbligatori = ["tool_mtr3", "relazione", "tool_mtr3_c", "relazione_c"]
    dich = ["dich_veridicita", "dich_veridicita_c"]

    all_obb = all(docs.get(k, {}).get("status") == "received" for k in obbligatori)
    all_dich = all(docs.get(k, {}).get("status") == "received" for k in dich)

    if all_obb and all_dich:
        return "si"
    elif all_obb:
        return "si_riserva"
    else:
        return "no"


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

import subprocess

COMMIT_EVERY = 5  # Commit intermedio ogni N comuni


def save_dashboard(dashboard, output_path, scan_time, results, total_comuni):
    """
    Salva il dashboard.json in modo atomico (write su file .tmp poi rename).
    Aggiorna il meta con lo stato corrente prima di salvare.
    Può essere chiamato dopo ogni comune per salvataggi incrementali.
    """
    dashboard["meta"] = {
        "last_scan": scan_time,
        "results": results,
        "total_comuni": total_comuni,
        "scadenze": SCADENZE,
        "scan_in_progress": results["scansionati"] + results["errori"] < total_comuni,
    }
    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(dashboard, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, output_path)


def merge_remote_dashboard(output_path, dashboard):
    """
    Legge il dashboard.json dal remote (via git fetch + git show) e lo fonde
    semanticamente con quello in memoria: per ogni comune vince la versione
    con last_scan più recente. Così nessun job sovrascrive i dati degli altri.
    Aggiorna anche il file su disco se c'è stato almeno un merge.
    """
    try:
        subprocess.run(["git", "fetch", "origin", "--quiet"], check=True, capture_output=True)
        result = subprocess.run(
            ["git", "show", f"origin/HEAD:{output_path}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return  # File non ancora presente sul remote, nulla da fondare
        remote = json.loads(result.stdout)
        merged = False
        for cid, remote_state in remote.get("comuni", {}).items():
            local_state = dashboard.get("comuni", {}).get(cid, {})
            if remote_state.get("last_scan", "") > local_state.get("last_scan", ""):
                dashboard.setdefault("comuni", {})[cid] = remote_state
                merged = True
        if merged:
            tmp = output_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dashboard, f, indent=2, ensure_ascii=False)
            os.replace(tmp, output_path)
            log_debug("Merge semantico con remote eseguito")
    except Exception as e:
        log_debug(f"WARN: merge remote fallito ({e}), continuo con versione locale")


def git_commit_push(output_path, processed, total, dashboard):
    """
    Esegue git add + commit + push del JSON aggiornato.
    Prima di ogni commit esegue un merge semantico con la versione remota
    per evitare di sovrascrivere dati di job concorrenti.
    Funziona solo in ambiente GitHub Actions (GITHUB_ACTIONS=true).
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return  # In locale non fa nulla

    try:
        # Merge semantico con il remote prima di committare
        merge_remote_dashboard(output_path, dashboard)

        subprocess.run(["git", "add", output_path], check=True)
        # Controlla se c'è qualcosa da committare
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            log_debug("Git: nessun cambiamento da committare")
            return

        msg = f"Scansione in corso [{processed}/{total} comuni]"
        subprocess.run(["git", "commit", "-m", msg], check=True)
        log_debug(f"Git commit: {msg}")

        # Push con retry in caso di conflitto
        for attempt in range(3):
            result = subprocess.run(["git", "push"], capture_output=True, text=True)
            if result.returncode == 0:
                log_debug("Git push OK")
                return
            log_debug(f"Git push fallito (tentativo {attempt+1}/3), remerge e riprova...")
            subprocess.run(["git", "pull", "--rebase"], capture_output=True)
            merge_remote_dashboard(output_path, dashboard)
            subprocess.run(["git", "add", output_path], check=True)
            subprocess.run(["git", "commit", "--amend", "--no-edit"], check=True)

        log_debug("WARN: git push fallito dopo 3 tentativi, continuo comunque")
    except subprocess.CalledProcessError as e:
        log_debug(f"WARN: git error: {e} — continuo comunque")


def main():
    parser = argparse.ArgumentParser(description="Scanner Dataroom ATA5 MTR3 2026")
    parser.add_argument("--credentials", required=True, help="Path al file credentials.json")
    parser.add_argument("--output", required=True, help="Path output dashboard.json")
    parser.add_argument("--filter", default="", help="Filtra per nome comune (parziale)")
    args = parser.parse_args()

    # Carica credenziali
    with open(args.credentials, "r", encoding="utf-8") as f:
        credentials = json.load(f)

    # Carica stato precedente
    dashboard = {"comuni": {}, "meta": {}, "scadenze": SCADENZE}
    if os.path.exists(args.output):
        with open(args.output, "r", encoding="utf-8") as f:
            dashboard = json.load(f)

    scan_time = datetime.now(timezone.utc).isoformat()
    download_dir = tempfile.mkdtemp(prefix="ata5_")

    # Filtra comuni se richiesto
    comuni = credentials
    if args.filter:
        comuni = [c for c in comuni if args.filter.lower() in c["comune"].lower()]

    log_debug("═══ Scanner ATA5 MTR3 2026 ═══")
    log_debug(f"Data: {scan_time}")
    log_debug(f"Comuni da scansionare: {len(comuni)}")

    results = {"scansionati": 0, "aggiornati": 0, "invariati": 0, "errori": 0, "vuoti": 0}

    for i, cred in enumerate(comuni, 1):
        cid = str(cred["id"])
        nome = cred["comune"]
        log_debug(f"[{i}/{len(comuni)}] === {nome} ===")

        old_state = dashboard.get("comuni", {}).get(cid, {})

        # Download (con retry automatico; non ritenta le dataroom vuote)
        zip_result = None
        for attempt in range(1 + MAX_RETRIES):
            if attempt > 0:
                log_debug(f"    Retry {attempt}/{MAX_RETRIES} per {nome}...")
                time.sleep(15)
            zip_result = download_zips(cred["url"], cred["pwd"], download_dir)
            if zip_result:  # lista, "EMPTY_DATAROOM": esci comunque
                break

        if zip_result == "EMPTY_DATAROOM":
            results["vuoti"] += 1
            dashboard.setdefault("comuni", {})[cid] = {
                **dashboard.get("comuni", {}).get(cid, {}),
                "last_scan": scan_time,
                "last_scan_error": False,
                "last_scan_empty": True,
                "info": {
                    "comune": nome,
                    "gestore": cred.get("gestore", ""),
                    "url": cred["url"],
                },
            }
            log_debug(f"    VUOTO: nessuna cartella MTR3 2026 ({nome})")
            processed = results["scansionati"] + results["errori"] + results["vuoti"]
            save_dashboard(dashboard, args.output, scan_time, results, len(credentials))
            if processed % COMMIT_EVERY == 0:
                git_commit_push(args.output, processed, len(comuni), dashboard)
            continue

        if not zip_result:
            results["errori"] += 1
            if cid not in dashboard.get("comuni", {}):
                dashboard.setdefault("comuni", {})[cid] = {}
            dashboard["comuni"][cid]["last_scan"] = scan_time
            dashboard["comuni"][cid]["last_scan_error"] = True
            dashboard["comuni"][cid]["last_scan_empty"] = False
            dashboard["comuni"][cid].setdefault("info", {})
            dashboard["comuni"][cid]["info"]["comune"] = nome
            dashboard["comuni"][cid]["info"]["gestore"] = cred.get("gestore", "")
            dashboard["comuni"][cid]["info"]["url"] = cred["url"]
            log_debug(f"    ERRORE su {nome} (dopo {1+MAX_RETRIES} tentativi)")
            processed = results["scansionati"] + results["errori"] + results["vuoti"]
            save_dashboard(dashboard, args.output, scan_time, results, len(credentials))
            if processed % COMMIT_EVERY == 0:
                git_commit_push(args.output, processed, len(comuni), dashboard)
            continue

        # Analizza i 3 (o meno) zip scaricati e fondili
        zip_hash, files = analyze_zips(zip_result)
        log_debug(f"    File totali (3 sotto-cartelle unite): {len(files)}")

        # Aggiorna stato
        new_state = update_comune_state(old_state, zip_hash, files, scan_time)
        new_state["info"] = {
            "comune": nome,
            "gestore": cred.get("gestore", ""),
            "advisor": cred.get("advisor", ""),
            "url": cred["url"],
            "id": cred["id"],
        }
        new_state["last_scan_error"] = False

        # Processabilità automatica
        new_state["processabile"] = compute_processabilita(new_state.get("docs", {}))

        dashboard.setdefault("comuni", {})[cid] = new_state
        results["scansionati"] += 1

        if new_state.get("zip_changed"):
            results["aggiornati"] += 1
            classified = sum(1 for f in files if f["classified"])
            unclassified = len(files) - classified
            log_debug(f"    AGGIORNATO! {classified} classificati, {unclassified} non classificati")
        else:
            results["invariati"] += 1
            log_debug("    Invariato")

        # Pulizia file zip scaricati
        for _, zp in zip_result:
            try:
                os.unlink(zp)
            except OSError:
                pass

        processed = results["scansionati"] + results["errori"]
        save_dashboard(dashboard, args.output, scan_time, results, len(credentials))
        if processed % COMMIT_EVERY == 0:
            git_commit_push(args.output, processed, len(comuni), dashboard)

    # ── Salvataggio finale ──
    save_dashboard(dashboard, args.output, scan_time, results, len(credentials))
    git_commit_push(args.output, len(comuni), len(comuni), dashboard)

    log_debug("═══ REPORT FINALE ═══")
    log_debug(f"Scansionati: {results['scansionati']}")
    log_debug(f"Aggiornati:  {results['aggiornati']}")
    log_debug(f"Invariati:   {results['invariati']}")
    log_debug(f"Vuoti:       {results['vuoti']}")
    log_debug(f"Errori:      {results['errori']}")


if __name__ == "__main__":
    main()
