# -*- coding: utf-8 -*-
"""
Rigenera i blob ENC_PWD_USERS e ENC_NKEY_USERS di docs/index.html con la
mappa delle password dataroom ATA5, mantenendo le stesse password di login
personali degli utenti GM, GN, ATA (Proposta A del porting da ATA4).

Uso:
    python scripts/gen_blobs.py

Lo script chiede interattivamente le 3 password di login (input nascosto,
non finisce in history), legge data/credentials.example.json, costruisce
per ciascun utente la mappa {urlKey: password_dataroom}, la cifra con
AES-256-GCM + PBKDF2-SHA256 (100.000 iter) usando la password di login
personale come chiave, e stampa i blob pronti da incollare in index.html.

Formato binario identico a _encryptText() lato JS:
    [salt 16 byte][iv 12 byte][ciphertext + tag]   → base64

La mappa password viene serializzata con JSON.stringify compatibile con JS:
chiavi e valori come semplici stringhe, nessuno spazio, separatori (",", ":").

La chiave AES delle note è 32 byte random, cifrata per ogni utente come
stringa raw (non JSON), così _tryDecryptRaw() la recupera come stringa.
"""

import json
import os
import base64
import getpass
import sys

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
except ImportError:
    print("ERRORE: manca la libreria 'cryptography'. Installa con:")
    print("    pip install cryptography")
    sys.exit(1)


PBKDF2_ITERATIONS = 100_000
KEY_LEN = 32  # AES-256


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt_blob(plaintext: bytes, password: str) -> str:
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = derive_key(password, salt)
    ct = AESGCM(key).encrypt(iv, plaintext, None)  # include il tag in coda
    out = salt + iv + ct
    return base64.b64encode(out).decode("ascii")


def url_key(url: str) -> str:
    """Replica _urlKey() di index.html: ultimo segmento del path."""
    return url.rstrip("/").split("/")[-1]


def main():
    # 1) carica credenziali ATA5
    creds_path = os.path.join("data", "credentials.example.json")
    if not os.path.exists(creds_path):
        print(f"ERRORE: non trovo {creds_path}. Lancia lo script dalla root del progetto.")
        sys.exit(1)
    with open(creds_path, "r", encoding="utf-8") as f:
        creds = json.load(f)

    # 2) costruisci mappa urlKey -> password dataroom
    pwd_map = {}
    for c in creds:
        k = url_key(c["url"])
        pwd_map[k] = c["pwd"]
    print(f"Caricate {len(pwd_map)} password dataroom ATA5.")

    # Serializzazione JSON compatibile con JS (separatori compatti, no spazi)
    pwd_map_json = json.dumps(pwd_map, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    # 3) genera nuova chiave AES-256 per le note (raw bytes → base64 string)
    notes_key_raw = base64.b64encode(os.urandom(32)).decode("ascii")
    print("Generata nuova chiave AES-256 per le note (base64).")

    # 4) chiedi le 3 password di login (stesse di ATA4 — Proposta A)
    users = ["GM", "GN", "ATA"]
    login_passwords = {}
    print()
    print("Inserisci le password di login ATA4 (saranno riutilizzate per ATA5).")
    print("L'input è nascosto, non viene salvato né loggato.")
    for u in users:
        while True:
            p = getpass.getpass(f"  Password login {u}: ")
            if p:
                login_passwords[u] = p
                break
            print("  (password vuota, riprova)")

    # 5) cifra mappa + chiave note per ciascun utente
    enc_pwd_users = {}
    enc_nkey_users = {}
    for u in users:
        enc_pwd_users[u] = encrypt_blob(pwd_map_json, login_passwords[u])
        enc_nkey_users[u] = encrypt_blob(notes_key_raw.encode("utf-8"), login_passwords[u])

    # 6) stampa i blob pronti da incollare
    print()
    print("═" * 70)
    print("BLOB GENERATI — copia e incolla in docs/index.html sostituendo")
    print("le costanti ENC_PWD_USERS e ENC_NKEY_USERS esistenti.")
    print("═" * 70)
    print()
    print("const ENC_PWD_USERS={")
    for u in users:
        comma = "," if u != users[-1] else ""
        print(f'  {u}:"{enc_pwd_users[u]}"{comma}')
    print("};")
    print()
    print("const ENC_NKEY_USERS={")
    for u in users:
        comma = "," if u != users[-1] else ""
        print(f'  {u}:"{enc_nkey_users[u]}"{comma}')
    print("};")
    print()

    # 7) salva anche su file per comodità (ignorato da git)
    out_path = os.path.join("data", "blobs_generated.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("const ENC_PWD_USERS={\n")
        for u in users:
            comma = "," if u != users[-1] else ""
            f.write(f'  {u}:"{enc_pwd_users[u]}"{comma}\n')
        f.write("};\n\n")
        f.write("const ENC_NKEY_USERS={\n")
        for u in users:
            comma = "," if u != users[-1] else ""
            f.write(f'  {u}:"{enc_nkey_users[u]}"{comma}\n')
        f.write("};\n")
    print(f"Blob salvati anche in {out_path} (già coperto da .gitignore 'data/').")
    print()
    print("Verifica rapida: prova a fare login sul cruscotto con una delle 3")
    print("password; dopo il login il pulsante 🔑 password di ogni comune")
    print("deve mostrare la password corretta della sua dataroom.")


if __name__ == "__main__":
    main()
