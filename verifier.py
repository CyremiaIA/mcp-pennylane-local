# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Test de bout en bout du serveur MCP local, tel que Claude le lancera.

Demarre pennylane_mcp.py en stdio, fait la poignee de main MCP, compte les
outils puis appelle pennylane_company_info (lecture seule, GET /me).
Ecrit un resume JSON sur stdout ; code de sortie 0 seulement si tout passe.

Usage : uv run --script verifier.py <uv> <chemin/pennylane_mcp.py>
"""

import json
import subprocess
import sys
import threading

ATTENDU = 29
DELAI = 180  # premier lancement : uv telecharge Python et les dependances


def main() -> int:
    uv, serveur = sys.argv[1], sys.argv[2]
    proc = subprocess.Popen(
        [uv, "run", "--quiet", "--script", serveur],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    reponses: dict = {}
    pret = {i: threading.Event() for i in (1, 2, 3)}

    def lire() -> None:
        for ligne in proc.stdout:
            try:
                msg = json.loads(ligne)
            except ValueError:
                continue
            if msg.get("id") in pret:
                reponses[msg["id"]] = msg
                pret[msg["id"]].set()

    threading.Thread(target=lire, daemon=True).start()

    def envoyer(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def attendre(i: int) -> dict | None:
        # Un serveur qui s'arrete (token absent...) ne doit pas faire attendre DELAI.
        for _ in range(DELAI * 4):
            if pret[i].wait(0.25):
                return reponses.get(i)
            if proc.poll() is not None:
                return reponses.get(i) if pret[i].wait(1) else None
        return None

    def cause_arret() -> str:
        if proc.poll() is None:
            return "Le serveur ne repond pas."
        lignes = [l.strip() for l in proc.stderr.read().splitlines() if l.strip()]
        return lignes[-1] if lignes else "Le serveur s'est arrete au demarrage."

    out: dict = {"ok": False}
    try:
        envoyer({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "verifier", "version": "1"}}})
        if not attendre(1):
            out["erreur"] = cause_arret()
            return 1
        envoyer({"jsonrpc": "2.0", "method": "notifications/initialized"})

        envoyer({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        r = attendre(2)
        outils = (r or {}).get("result", {}).get("tools", [])
        out["outils"] = len(outils)
        if len(outils) != ATTENDU:
            out["erreur"] = f"{len(outils)} outils au lieu de {ATTENDU}."
            return 1

        envoyer({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "pennylane_company_info", "arguments": {}}})
        r = attendre(3) or {}
        res = r.get("result", {})
        donnees = res.get("structuredContent")
        if not isinstance(donnees, dict):
            texte = "".join(c.get("text", "") for c in res.get("content", []))
            try:
                donnees = json.loads(texte)
            except ValueError:
                donnees = {"erreur": texte or r.get("error", "reponse vide")}
        donnees = donnees.get("result", donnees) if isinstance(donnees, dict) else {}
        if "erreur" in donnees:
            out["erreur"] = f"Pennylane refuse : {donnees.get('erreur')}"
            return 1

        societe = donnees.get("company") or {}
        out["societe"] = societe.get("name", "") if isinstance(societe, dict) else ""
        out["scopes"] = donnees.get("scopes") or []
        out["ok"] = True
        return 0
    finally:
        proc.kill()
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
