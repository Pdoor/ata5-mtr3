#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
#  history_tracker.py
#  ───────────────────
#  Storicizza l'evoluzione dei file caricati nelle cartelle
#      "Eventuali - Comunicazioni supplementari"  (Gestore e Comune)
#  che lo scanner principale NON classifica come allegati MTR3.
#
#  L'utilizzo tipico è come step aggiuntivo del workflow GitHub Actions,
#  eseguito DOPO `scanner.py`. È completamente READ-ONLY su `dashboard.json`:
#  legge i file da `all_files[]` di ogni comune, filtra quelli che si
#  trovano nelle cartelle "Eventuali - Comunicazioni supplementari", li
#  confronta con lo stato precedentemente memorizzato in
#  `eventuali_history.json` e produce una nuova versione del file con
#  eventi puntuali: primo_caricamento / sostituzione / rimosso.
#
#  Vantaggi del file separato:
#    • zero impatto su scanner.py e dashboard.json
#    • zero credenziali aggiuntive (lavora solo su file locali)
#    • universale per ATA4 e ATA5 (regex tollerante sul path)
#    • idempotente: rieseguirlo non altera la storia
#
#  Schema di eventuali_history.json:
#    {
#      "meta": {
#        "version": 1,
#        "last_update": "2026-04-27T12:00:00+00:00",
#        "dashboard_last_scan": "...",
#        "files_tracked": 12
#      },
#      "comuni": {
#        "<id>": {
#          "gestore": [ <file_record>, ... ],
#          "comune":  [ <file_record>, ... ]
#        }
#      }
#    }
#
#  <file_record>:
#    {
#      "filename": "<nome file>",
#      "path": "Gestore/Eventuali .../<nome>",
#      "current_hash": "...",
#      "current_size": <int>,
#      "first_seen": "<iso>",
#      "last_seen":  "<iso>",
#      "status": "presente" | "rimosso",
#      "removed_at": "<iso>" | null,
#      "history": [
#        {"event": "primo_caricamento", "time": "...", "hash": "...", "size": <int>},
#        {"event": "sostituzione",      "time": "...", "hash": "...", "size": <int>, "previous_hash": "..."},
#        {"event": "rimosso",           "time": "...", "previous_hash": "..."}
#      ]
#    }
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import datetime as _dt
import json
import os
import re
import sys

VERSION = 1

# Regex tollerante per il path delle cartelle "Eventuali - Comunicazioni supplementari".
# Accetta sia il formato ATA4  ("Gestore/Eventuali - ...") sia ATA5
# ("Gestore 2026/Eventuali - ...") con eventuali variazioni di spazi.
EVENTUALI_RE = re.compile(
    r"^(?P<source>Gestore|Comune)(?:\s+\d{4})?/Eventuali\s*-\s*Comunicazioni\s+supplementari/",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[history_tracker] Errore lettura {path}: {e}", file=sys.stderr)
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _iso_now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _empty_history():
    return {
        "meta": {
            "version": VERSION,
            "last_update": None,
            "dashboard_last_scan": None,
            "files_tracked": 0,
        },
        "comuni": {},
    }


def _classify_eventuali_file(file_entry):
    """
    Ritorna ("gestore"|"comune", filename) se il file appartiene a una
    cartella "Eventuali - Comunicazioni supplementari", altrimenti None.
    Si basa esclusivamente sul `path` per evitare bug noti dove il campo
    `source` viene riempito in modo non affidabile dallo scanner per i
    file non classificati.
    """
    path = (file_entry or {}).get("path") or ""
    if not path:
        return None
    m = EVENTUALI_RE.match(path)
    if not m:
        return None
    src = m.group("source").lower()
    if src not in ("gestore", "comune"):
        return None
    name = (file_entry.get("name") or "").strip()
    if not name:
        # fallback: deriva il filename dal path
        name = path.rstrip("/").split("/")[-1]
    return src, name, path


# ─────────────────────────────────────────────────────────────────────────────
#  Core
# ─────────────────────────────────────────────────────────────────────────────

def _find_record(records, filename):
    """Trova il record con quel filename (case-sensitive)."""
    for r in records:
        if r.get("filename") == filename:
            return r
    return None


def _update_comune(prev_records, current_files, comune_last_scan, scan_time):
    """
    Aggiorna la lista di record per UNA singola fonte (gestore/comune)
    di un comune. `prev_records` è la lista esistente (può essere vuota),
    `current_files` è la lista dei file "Eventuali" trovati ORA per quella
    fonte. Ritorna la nuova lista.

    Logica di aggiornamento:
      - Per ogni file corrente:
          - se non esisteva nei record o era marcato "rimosso": evento
            "primo_caricamento" (oppure ricomparsa).
          - se esisteva con stesso hash: aggiorna last_seen, niente evento.
          - se esisteva con hash diverso: evento "sostituzione".
      - Per ogni record precedente non più presente fra i correnti, e
        attualmente "presente": evento "rimosso".
      - I record "rimosso" non più presenti vengono mantenuti (storia).
    """
    # Indice per lookup veloce, ma manteniamo la lista per preservare ordine
    out = []
    seen_filenames = set()

    # 1) processa i file correnti
    for f in current_files:
        filename = f["filename"]
        seen_filenames.add(filename)

        new_hash = f.get("hash") or ""
        new_size = f.get("size") or 0
        new_path = f.get("path") or ""

        prev = _find_record(prev_records, filename)
        if prev is None:
            # Mai visto prima
            rec = {
                "filename": filename,
                "path": new_path,
                "current_hash": new_hash,
                "current_size": new_size,
                "first_seen": comune_last_scan or scan_time,
                "last_seen": comune_last_scan or scan_time,
                "status": "presente",
                "removed_at": None,
                "history": [{
                    "event": "primo_caricamento",
                    "time": comune_last_scan or scan_time,
                    "hash": new_hash,
                    "size": new_size,
                }],
            }
            out.append(rec)
            continue

        # Esisteva
        rec = json.loads(json.dumps(prev))  # deep copy
        prev_status = rec.get("status", "presente")
        prev_hash = rec.get("current_hash") or ""

        if prev_status == "rimosso":
            # Ricomparsa: la trattiamo come nuovo caricamento con history continua
            rec["status"] = "presente"
            rec["removed_at"] = None
            rec["current_hash"] = new_hash
            rec["current_size"] = new_size
            rec["path"] = new_path
            rec["last_seen"] = comune_last_scan or scan_time
            history = rec.get("history") or []
            event = "primo_caricamento" if not prev_hash else "sostituzione"
            entry = {
                "event": event,
                "time": comune_last_scan or scan_time,
                "hash": new_hash,
                "size": new_size,
            }
            if event == "sostituzione":
                entry["previous_hash"] = prev_hash
            history.append(entry)
            rec["history"] = history[-50:]
        elif new_hash and prev_hash and new_hash != prev_hash:
            # Sostituzione (hash cambiato)
            rec["current_hash"] = new_hash
            rec["current_size"] = new_size
            rec["path"] = new_path
            rec["last_seen"] = comune_last_scan or scan_time
            history = rec.get("history") or []
            history.append({
                "event": "sostituzione",
                "time": comune_last_scan or scan_time,
                "hash": new_hash,
                "size": new_size,
                "previous_hash": prev_hash,
            })
            rec["history"] = history[-50:]
        else:
            # Invariato: aggiorna solo last_seen e (eventualmente) current_size/path
            rec["last_seen"] = comune_last_scan or scan_time
            if new_size:
                rec["current_size"] = new_size
            if new_path:
                rec["path"] = new_path
            # Se prev_hash era vuoto e ora abbiamo un hash, registralo (no event nuovo)
            if not prev_hash and new_hash:
                rec["current_hash"] = new_hash

        out.append(rec)

    # 2) gestisci file scomparsi
    for prev in prev_records:
        filename = prev.get("filename")
        if not filename or filename in seen_filenames:
            continue
        rec = json.loads(json.dumps(prev))  # deep copy
        if rec.get("status") == "presente":
            # Ora è scomparso → evento "rimosso"
            rec["status"] = "rimosso"
            rec["removed_at"] = comune_last_scan or scan_time
            history = rec.get("history") or []
            history.append({
                "event": "rimosso",
                "time": comune_last_scan or scan_time,
                "previous_hash": rec.get("current_hash") or "",
            })
            rec["history"] = history[-50:]
        # Manteniamo comunque il record nella lista (storia)
        out.append(rec)

    return out


def update_history(dashboard, prev_history):
    """
    Costruisce la nuova storia partendo da `dashboard` e `prev_history`.
    """
    new_history = _empty_history()
    # Manteniamo i comuni precedenti come baseline (per non perdere storia
    # di comuni eventualmente non scansionati in questa run o filtrati via)
    new_history["comuni"] = json.loads(json.dumps(prev_history.get("comuni") or {}))

    scan_time = _iso_now()
    dashboard_meta = dashboard.get("meta") or {}
    new_history["meta"]["dashboard_last_scan"] = dashboard_meta.get("last_scan")
    new_history["meta"]["last_update"] = scan_time

    comuni = dashboard.get("comuni") or {}
    files_tracked = 0

    for cid, cstate in comuni.items():
        if not isinstance(cstate, dict):
            continue
        all_files = cstate.get("all_files") or []
        comune_last_scan = cstate.get("last_scan") or scan_time

        # Raccoglie i file "Eventuali" del comune, divisi per source
        bucket = {"gestore": [], "comune": []}
        for f in all_files:
            cls = _classify_eventuali_file(f)
            if cls is None:
                continue
            src, filename, path = cls
            bucket[src].append({
                "filename": filename,
                "path": path,
                "hash": f.get("hash") or "",
                "size": f.get("size") or 0,
            })

        # Stato precedente per questo comune
        prev_cmn = (new_history["comuni"].get(cid)
                    or {"gestore": [], "comune": []})
        if not isinstance(prev_cmn, dict):
            prev_cmn = {"gestore": [], "comune": []}

        new_cmn = {
            "gestore": _update_comune(
                prev_cmn.get("gestore") or [],
                bucket["gestore"],
                comune_last_scan,
                scan_time,
            ),
            "comune": _update_comune(
                prev_cmn.get("comune") or [],
                bucket["comune"],
                comune_last_scan,
                scan_time,
            ),
        }

        # Conta i file attualmente "presenti"
        for src in ("gestore", "comune"):
            for r in new_cmn[src]:
                if r.get("status") == "presente":
                    files_tracked += 1

        # Salva solo se c'è almeno un record (anche storico) per il comune,
        # altrimenti rimuoviamo l'eventuale chiave vuota residua
        if new_cmn["gestore"] or new_cmn["comune"]:
            new_history["comuni"][cid] = new_cmn
        else:
            new_history["comuni"].pop(cid, None)

    new_history["meta"]["files_tracked"] = files_tracked
    return new_history


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description=(
            'Storicizza i file caricati nelle cartelle "Eventuali - '
            'Comunicazioni supplementari" leggendo dashboard.json (read-only) '
            'e aggiornando eventuali_history.json.'
        ),
    )
    p.add_argument("--dashboard", required=True, help="Percorso a dashboard.json (input, read-only)")
    p.add_argument("--history", required=True, help="Percorso a eventuali_history.json (input/output)")
    p.add_argument("--dry-run", action="store_true", help="Non scrive l'output, mostra solo cosa cambierebbe")
    args = p.parse_args()

    if not os.path.exists(args.dashboard):
        print(f"[history_tracker] dashboard.json non trovato: {args.dashboard}", file=sys.stderr)
        return 1

    dashboard = _load_json(args.dashboard, default=None)
    if not isinstance(dashboard, dict):
        print("[history_tracker] dashboard.json non è un oggetto JSON valido", file=sys.stderr)
        return 1

    prev_history = _load_json(args.history, default=_empty_history())
    if not isinstance(prev_history, dict) or "comuni" not in prev_history:
        prev_history = _empty_history()

    new_history = update_history(dashboard, prev_history)

    # Decidi se c'è una variazione effettiva (ignorando solo last_update / files_tracked)
    def _normalize_for_diff(h):
        c = json.loads(json.dumps(h))
        c.get("meta", {}).pop("last_update", None)
        c.get("meta", {}).pop("files_tracked", None)
        return c

    changed = _normalize_for_diff(prev_history) != _normalize_for_diff(new_history)

    print(f"[history_tracker] Comuni in dashboard: {len(dashboard.get('comuni') or {})}")
    print(f"[history_tracker] File 'Eventuali' attualmente presenti: {new_history['meta']['files_tracked']}")
    print(f"[history_tracker] Variazione storica rilevata: {'SI' if changed else 'NO'}")

    if args.dry_run:
        print("[history_tracker] DRY-RUN: nessuna scrittura.")
        return 0

    if not changed and os.path.exists(args.history):
        # Aggiorniamo solo last_update senza riscrivere se non ci sono variazioni
        # — riscriviamo comunque per allineare last_update e files_tracked
        pass

    _save_json(args.history, new_history)
    print(f"[history_tracker] Scritto: {args.history}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
