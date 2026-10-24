#!/bin/bash
# =============================================================================
# Installateur du connecteur Pennylane pour Claude (macOS) — Cyremia
#
#   curl -fsSL https://raw.githubusercontent.com/CyremiaIA/mcp-pennylane-local/main/install.sh | bash
#
# Desinstallation :
#   curl -fsSL https://raw.githubusercontent.com/CyremiaIA/mcp-pennylane-local/main/install.sh | bash -s -- --desinstaller
#
# Ce que fait ce script, sans droits administrateur :
#   1. telecharge gum (interface) et uv (Python) dans un dossier dedie, versions
#      epinglees et empreintes SHA-256 verifiees ;
#   2. range le token API Pennylane dans le trousseau macOS (jamais en clair) ;
#   3. declare le serveur dans Claude Desktop (et Claude Code s'il est present) ;
#   4. teste le serveur exactement comme Claude le lancera.
#
# Tout est dans « ~/Library/Application Support/Cyremia/pennylane-mcp ».
# Journal (sans le token) : ~/Library/Logs/Cyremia/installation-pennylane-mcp.log
# =============================================================================

set -euo pipefail

SOURCE="${MCP_PL_SOURCE:-https://raw.githubusercontent.com/CyremiaIA/mcp-pennylane-local/main}"

GUM_VERSION="0.17.0"
GUM_SHA_arm64="e2a4b8596efa05821d8c58d0c1afbcd7ad1699ba69c689cc3ff23a4a99c8b237"
GUM_SHA_x86_64="cd66576aeebe6cd19c771863c7e8d696e0e1d5387d1e7075666baa67c2052e53"
UV_VERSION="0.12.24"
UV_SHA_aarch64="0c4346de7abdb49495b393b9ec809fe387aa43e586be20fecb972216c1e71732"
UV_SHA_x86_64="4fa82e37cb94767661f532b001e470b67a186c7260e305bd84ddb78fd545c0b6"

DOSSIER="$HOME/Library/Application Support/Cyremia/pennylane-mcp"
BIN="$DOSSIER/bin"
SERVEUR="$DOSSIER/pennylane_mcp.py"
VERIFIER="$DOSSIER/verifier.py"
LOG="$HOME/Library/Logs/Cyremia/installation-pennylane-mcp.log"
CONFIG_CLAUDE="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
TROUSSEAU="cyremia-pennylane-mcp"
NOM_MCP="pennylane"
SUPPORT="technique@cyremia.com"
PENNYLANE_API="https://app.pennylane.com/api/external/v2"

GUM="$BIN/gum"
UV="$BIN/uv"
# uv garde Python et son cache dans notre dossier : desinstaller = le supprimer.
export UV_CACHE_DIR="$DOSSIER/cache"
export UV_PYTHON_INSTALL_DIR="$DOSSIER/python"
# Jamais le Python du systeme : sur un Mac sans outils de developpement,
# /usr/bin/python3 ouvre une fenetre d'installation au lieu de s'executer.
export UV_PYTHON_PREFERENCE="only-managed"

ACCENT="39"
ETAPES=5

# -----------------------------------------------------------------------------
# Outils d'affichage
# -----------------------------------------------------------------------------

journal() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }

titre_etape() {  # titre_etape <n> <texte>
  echo
  "$GUM" style --foreground "$ACCENT" --bold "Étape $1/$ETAPES · $2"
  journal "== Étape $1 : $2"
}

info()   { "$GUM" style --margin "0 2" "$@"; }
succes() { "$GUM" style --margin "0 2" --foreground 42 "✓ $*"; journal "OK $*"; }
alerte() { "$GUM" style --margin "0 2" --foreground 214 "⚠ $*"; journal "ALERTE $*"; }

encadre() {  # encadre <couleur> <lignes...>
  local couleur="$1"; shift
  "$GUM" style --border rounded --border-foreground "$couleur" \
    --padding "1 2" --margin "1 0" --width 70 "$@"
}

attendre() {  # attendre <titre> <commande...> : spinner, sortie dans le journal
  local t="$1"; shift
  journal "\$ $*"
  "$GUM" spin --spinner dot --title " $t" -- "$@" >>"$LOG" 2>&1
}

echec() {
  journal "ECHEC $*"
  if [ -x "$GUM" ]; then
    encadre 196 \
      "$("$GUM" style --bold --foreground 196 "L'installation n'a pas abouti")" "" \
      "$*" "" \
      "Rien de grave : vous pouvez relancer la même commande." \
      "Si cela recommence, envoyez le fichier journal à $SUPPORT :" \
      "$LOG"
    if "$GUM" confirm "Afficher le fichier journal dans le Finder ?" \
        --affirmative "Oui" --negative "Non"; then
      open -R "$LOG" || true
    fi
  else
    printf '\nL installation n a pas abouti : %s\nJournal : %s\nSupport : %s\n' "$*" "$LOG" "$SUPPORT" >&2
  fi
  exit 1
}

telecharger() {  # telecharger <url> <destination>
  curl -fsSL --retry 3 --connect-timeout 15 -o "$2" "$1"
}

verifier_empreinte() {  # verifier_empreinte <fichier> <sha256 attendu>
  local obtenu
  obtenu="$(shasum -a 256 "$1" | awk '{print $1}')"
  [ "$obtenu" = "$2" ] || { journal "empreinte $1 : $obtenu au lieu de $2"; return 1; }
}

# -----------------------------------------------------------------------------
# Amorçage : gum puis uv, sans rien demander
# -----------------------------------------------------------------------------

installer_gum() {
  [ -x "$GUM" ] && "$GUM" --version 2>/dev/null | grep -q "$GUM_VERSION" && return 0
  local arch sha tmp
  case "$(uname -m)" in
    arm64)  arch="arm64";  sha="$GUM_SHA_arm64" ;;
    x86_64) arch="x86_64"; sha="$GUM_SHA_x86_64" ;;
    *) echec "Processeur non pris en charge : $(uname -m)" ;;
  esac
  tmp="$(mktemp -d)"
  telecharger "https://github.com/charmbracelet/gum/releases/download/v${GUM_VERSION}/gum_${GUM_VERSION}_Darwin_${arch}.tar.gz" "$tmp/gum.tgz" \
    || echec "Téléchargement impossible. Vérifiez votre connexion Internet."
  verifier_empreinte "$tmp/gum.tgz" "$sha" || echec "Fichier téléchargé corrompu (gum)."
  tar -xzf "$tmp/gum.tgz" -C "$tmp"
  install -m 0755 "$tmp/gum_${GUM_VERSION}_Darwin_${arch}/gum" "$GUM"
  rm -rf "$tmp"
}

installer_uv() {
  [ -x "$UV" ] && "$UV" --version 2>/dev/null | grep -q "$UV_VERSION" && return 0
  local arch sha tmp
  case "$(uname -m)" in
    arm64)  arch="aarch64"; sha="$UV_SHA_aarch64" ;;
    x86_64) arch="x86_64";  sha="$UV_SHA_x86_64" ;;
  esac
  tmp="$(mktemp -d)"
  telecharger "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${arch}-apple-darwin.tar.gz" "$tmp/uv.tgz"
  verifier_empreinte "$tmp/uv.tgz" "$sha"
  tar -xzf "$tmp/uv.tgz" -C "$tmp"
  install -m 0755 "$tmp/uv-${arch}-apple-darwin/uv" "$UV"
  rm -rf "$tmp"
}

# Exporte pour gum spin, qui lance la commande dans un nouveau processus.
export -f installer_uv telecharger verifier_empreinte journal
export UV UV_VERSION UV_SHA_aarch64 UV_SHA_x86_64 LOG

# -----------------------------------------------------------------------------
# Claude
# -----------------------------------------------------------------------------

claude_present() {
  [ -d "/Applications/Claude.app" ] || [ -d "$HOME/Applications/Claude.app" ]
}

claude_lance() { pgrep -xq "Claude"; }

fermer_claude() {
  osascript -e 'quit app "Claude"' >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do claude_lance || return 0; sleep 0.5; done
  return 1
}

# Ajoute (ou retire, avec --retirer) notre entree dans la config de Claude
# Desktop, sans toucher aux autres serveurs. Refuse d'ecraser un JSON illisible.
config_claude_desktop() {
  "$UV" run --quiet --no-project python - "$CONFIG_CLAUDE" "$NOM_MCP" "${1:-}" \
    "$UV" "$SERVEUR" "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" "$UV_PYTHON_PREFERENCE" <<'PY'
import json, os, sys, shutil, time
chemin, nom, mode, uv, serveur, cache, python, preference = sys.argv[1:9]
config = {}
if os.path.exists(chemin):
    with open(chemin, encoding="utf-8") as f:
        texte = f.read()
    if texte.strip():
        config = json.loads(texte)  # leve si illisible : on n'ecrase rien
    shutil.copy2(chemin, chemin + time.strftime(".sauvegarde-%Y%m%d-%H%M%S"))
serveurs = config.setdefault("mcpServers", {})
if mode == "--retirer":
    serveurs.pop(nom, None)
else:
    serveurs[nom] = {
        "command": uv,
        "args": ["run", "--quiet", "--script", serveur],
        "env": {"UV_CACHE_DIR": cache, "UV_PYTHON_INSTALL_DIR": python,
                "UV_PYTHON_PREFERENCE": preference},
    }
os.makedirs(os.path.dirname(chemin), exist_ok=True)
tmp = chemin + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, chemin)
PY
}

claude_code() { command -v claude >/dev/null 2>&1; }

# -----------------------------------------------------------------------------
# Token Pennylane
# -----------------------------------------------------------------------------

token_trousseau() {
  security find-generic-password -s "$TROUSSEAU" -w 2>/dev/null || true
}

lire_me() {  # lire_me <fichier> -> "societe<TAB>lecture_seule(oui|non)"
  "$UV" run --quiet --no-project python - "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
societe = (d.get("company") or {}).get("name") or "?"
scopes = d.get("scopes") or []
ecriture = any(s.startswith(("quotes:", "products:")) and s.endswith(":all") for s in scopes)
print(f"{societe}\t{'non' if ecriture else 'oui'}")
PY
}

demander_token() {
  encadre "$ACCENT" \
    "$("$GUM" style --bold "Créer votre token Pennylane")" "" \
    "1. Ouvrez Pennylane, puis Paramètres → Connectivité → Développeurs" \
    "2. Cliquez sur « Générer un token API »" \
    "3. Nom : Claude  ·  Version : API V2" \
    "4. Droits : « Lecture et écriture »" \
    "5. Durée : 12 mois (notez la date, il faudra le renouveler)" \
    "6. Copiez le token : Pennylane ne l'affiche qu'une seule fois" "" \
    "$("$GUM" style --faint "Il faut un compte administrateur et une offre Essential ou supérieure.")"

  if "$GUM" confirm "Ouvrir Pennylane dans votre navigateur ?" --affirmative "Oui" --negative "Non, c'est déjà fait"; then
    open "https://app.pennylane.com/" || true
  fi

  local essai reponse code infos
  reponse="$(mktemp)"
  for essai in 1 2 3; do
    echo
    info "Collez votre token ci-dessous (⌘ + V), puis appuyez sur Entrée."
    info "$("$GUM" style --faint "Rien ne s'affiche pendant la saisie : c'est normal.")"
    TOKEN="$("$GUM" input --password --width 60 --placeholder "Token Pennylane…" --prompt "  › ")" || echec "Saisie annulée."
    TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
    if [ -z "$TOKEN" ]; then alerte "Aucun token collé."; continue; fi

    # GET /me ne demande aucun scope. Le token passe par l'environnement puis
    # par un descripteur : jamais en argument, donc jamais visible dans ps.
    code="$(TOKEN_PL="$TOKEN" "$GUM" spin --spinner dot --title " Vérification auprès de Pennylane…" -- \
      bash -c 'curl -sS --connect-timeout 15 -o "$1" -w "%{http_code}" -H @<(printf "Authorization: Bearer %s\n" "$TOKEN_PL") -H "Accept: application/json" "$2/me" 2>/dev/null || echo 000' \
      _ "$reponse" "$PENNYLANE_API")"
    journal "GET /me -> HTTP $code (essai $essai)"
    case "$code" in
      200)
        infos="$(lire_me "$reponse")" || infos=$'?\tnon'
        SOCIETE="${infos%%$'\t'*}"
        LECTURE_SEULE="${infos##*$'\t'}"
        rm -f "$reponse"
        succes "Token accepté : société « $SOCIETE »"
        if [ "$LECTURE_SEULE" = "oui" ]; then
          alerte "Ce token est en lecture seule : Claude pourra consulter, mais pas créer de devis ni de clients."
          "$GUM" confirm "Continuer quand même ?" --affirmative "Oui" --negative "Non, je refais un token" \
            || continue
        fi
        return 0 ;;
      401|403) alerte "Pennylane refuse ce token (HTTP $code). Vérifiez qu'il est copié en entier, sans espace." ;;
      000)     alerte "Pennylane ne répond pas. Vérifiez votre connexion Internet." ;;
      *)       alerte "Réponse inattendue de Pennylane (HTTP $code)." ;;
    esac
  done
  rm -f "$reponse"
  echec "Le token n'a pas pu être validé après 3 essais."
}

ranger_token() {
  # -U met a jour si l'entree existe deja. -T autorise /usr/bin/security a la
  # relire sans fenetre de confirmation (c'est lui que le serveur appelle).
  # -w en dernier sans valeur : le token est lu sur l'entree standard (deux
  # fois, saisie + confirmation), jamais passe en argument.
  printf '%s\n%s\n' "$TOKEN" "$TOKEN" | security add-generic-password -U -a "$USER" -s "$TROUSSEAU" \
    -l "Pennylane (connecteur Claude)" -T /usr/bin/security -w >>"$LOG" 2>&1 \
    || echec "Impossible d'enregistrer le token dans le trousseau macOS."
  unset TOKEN
  succes "Token rangé dans le trousseau macOS (« Pennylane (connecteur Claude) »)"
}

# -----------------------------------------------------------------------------
# Parcours
# -----------------------------------------------------------------------------

installer() {
  encadre "$ACCENT" \
    "$("$GUM" style --bold --foreground "$ACCENT" "Connecteur Pennylane pour Claude")" \
    "$("$GUM" style --faint "proposé par Cyremia")" "" \
    "Claude pourra lire votre comptabilité Pennylane et préparer" \
    "devis, clients et produits, toujours avec votre accord." "" \
    "Durée : environ 5 minutes. Il vous faudra :" \
    "  • l'application Claude installée sur ce Mac ;" \
    "  • un accès administrateur à Pennylane." "" \
    "Tout reste sur votre Mac : aucun serveur intermédiaire."
  "$GUM" confirm "On commence ?" --affirmative "C'est parti" --negative "Plus tard" \
    || { info "D'accord, à bientôt !"; exit 0; }

  # 1 — Préparation
  titre_etape 1 "Préparation"
  claude_present || echec "L'application Claude est introuvable dans Applications. Installez-la depuis claude.ai/download, puis relancez cette commande."
  succes "Application Claude trouvée"
  attendre "Installation des outils…" bash -c installer_uv || echec "Impossible d'installer uv."
  attendre "Téléchargement du connecteur…" bash -c '
    telecharger "$1/pennylane_mcp.py" "$2.tmp" && mv "$2.tmp" "$2" &&
    telecharger "$1/verifier.py" "$3.tmp" && mv "$3.tmp" "$3"' _ "$SOURCE" "$SERVEUR" "$VERIFIER" \
    || echec "Téléchargement du connecteur impossible."
  attendre "Préparation de Python (1 à 2 minutes la première fois)…" \
    "$UV" sync --quiet --script "$SERVEUR" || echec "Préparation de Python impossible."
  succes "Outils prêts"

  # 2 — Token
  titre_etape 2 "Votre token Pennylane"
  local existant
  existant="$(token_trousseau)"
  if [ -n "$existant" ] && "$GUM" confirm "Un token est déjà enregistré sur ce Mac. Le garder ?" \
      --affirmative "Oui, le garder" --negative "Non, en saisir un nouveau"; then
    succes "Token existant conservé"
  else
    demander_token
    ranger_token
  fi
  unset existant

  # 3 — Claude
  titre_etape 3 "Ajout dans Claude"
  RELANCER=0
  if claude_lance; then
    info "Claude doit être fermé quelques secondes pour ajouter Pennylane."
    info "$("$GUM" style --faint "Vos conversations sont conservées.")"
    "$GUM" confirm "Fermer Claude maintenant ?" --affirmative "Oui" --negative "Annuler" \
      || echec "Installation interrompue : Claude doit être fermé pour ajouter le connecteur."
    fermer_claude || echec "Claude ne s'est pas fermé. Quittez-le (⌘ + Q), puis relancez la commande."
    RELANCER=1
  fi
  config_claude_desktop >>"$LOG" 2>&1 \
    || echec "La configuration de Claude est illisible : rien n'a été modifié."
  succes "Pennylane ajouté à Claude Desktop"
  if claude_code; then
    claude mcp remove --scope user "$NOM_MCP" >>"$LOG" 2>&1 || true
    if claude mcp add --scope user "$NOM_MCP" \
        -e "UV_CACHE_DIR=$UV_CACHE_DIR" -e "UV_PYTHON_INSTALL_DIR=$UV_PYTHON_INSTALL_DIR" \
        -e "UV_PYTHON_PREFERENCE=$UV_PYTHON_PREFERENCE" \
        -- "$UV" run --quiet --script "$SERVEUR" >>"$LOG" 2>&1; then
      succes "Pennylane ajouté aussi à Claude Code"
    else
      alerte "Claude Code détecté mais l'ajout a échoué (sans conséquence pour Claude Desktop)."
    fi
  fi

  # 4 — Test
  titre_etape 4 "Test de fonctionnement"
  local resultat
  resultat="$(mktemp)"
  "$GUM" spin --spinner dot --title " Claude lance le connecteur et interroge Pennylane…" -- \
    bash -c '"$1" run --quiet --script "$2" "$1" "$3" >"$4" 2>>"$5"' _ "$UV" "$VERIFIER" "$SERVEUR" "$resultat" "$LOG" \
    || true
  journal "verifier : $(cat "$resultat")"
  local bilan
  bilan="$("$UV" run --quiet --no-project python - "$resultat" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    d = {"ok": False, "erreur": "aucune reponse du test"}
if d.get("ok"):
    print(f"OK\t{d.get('outils')} outils disponibles, société « {d.get('societe') or '?'} »")
else:
    print(f"KO\t{d.get('erreur', 'erreur inconnue')}")
PY
)"
  rm -f "$resultat"
  [ "${bilan%%$'\t'*}" = "OK" ] || echec "Le test a échoué : ${bilan#*$'\t'}"
  succes "${bilan#*$'\t'}"

  # 5 — Fin
  titre_etape 5 "C'est prêt"
  if [ "$RELANCER" = 1 ] || "$GUM" confirm "Ouvrir Claude maintenant ?" --affirmative "Oui" --negative "Non"; then
    open -a Claude || true
  fi
  encadre 42 \
    "$("$GUM" style --bold --foreground 42 "Pennylane est connecté à Claude ✓")" "" \
    "Dans Claude, essayez par exemple :" \
    "$("$GUM" style --italic "  « Quels sont mes 5 derniers devis dans Pennylane ? »")" \
    "$("$GUM" style --italic "  « Quelles factures clients sont encore impayées ? »")" "" \
    "Claude vous demandera l'autorisation avant d'utiliser Pennylane." \
    "Pour une lecture, « Toujours autoriser » est confortable ;" \
    "pour une création ou une modification, relisez avant d'accepter." "" \
    "$("$GUM" style --faint "Une question ? $SUPPORT")"
}

desinstaller() {
  encadre 214 \
    "$("$GUM" style --bold "Désinstaller le connecteur Pennylane")" "" \
    "Le connecteur, son token et ses fichiers seront retirés de ce Mac." \
    "Vos données Pennylane ne sont pas touchées."
  "$GUM" confirm "Confirmer la désinstallation ?" --affirmative "Désinstaller" --negative "Annuler" \
    || { info "Rien n'a été modifié."; exit 0; }

  local relancer=0
  if claude_lance; then
    "$GUM" confirm "Claude doit être fermé quelques secondes. Le fermer ?" --affirmative "Oui" --negative "Annuler" \
      || { info "Rien n'a été modifié."; exit 0; }
    fermer_claude || echec "Claude ne s'est pas fermé. Quittez-le (⌘ + Q), puis relancez la commande."
    relancer=1
  fi
  if [ -f "$CONFIG_CLAUDE" ]; then
    config_claude_desktop --retirer >>"$LOG" 2>&1 || alerte "Configuration de Claude illisible : entrée « $NOM_MCP » à retirer à la main."
  fi
  succes "Retiré de Claude Desktop"
  if claude_code; then
    claude mcp remove --scope user "$NOM_MCP" >>"$LOG" 2>&1 && succes "Retiré de Claude Code" || true
  fi
  security delete-generic-password -s "$TROUSSEAU" >>"$LOG" 2>&1 && succes "Token retiré du trousseau" || true
  # gum vit dans le dossier supprime : on le copie le temps du message final.
  local gum_tmp
  gum_tmp="$(mktemp -d)/gum"
  cp "$GUM" "$gum_tmp"
  GUM="$gum_tmp"
  rm -rf "$DOSSIER"
  succes "Fichiers supprimés"
  [ "$relancer" = 1 ] && open -a Claude || true
  info "Pensez à révoquer le token dans Pennylane (Paramètres → Connectivité → Développeurs)."
  rm -rf "$(dirname "$gum_tmp")"
}

main() {
  [ "$(uname -s)" = "Darwin" ] || { echo "Cet installateur est prévu pour macOS." >&2; exit 1; }
  mkdir -p "$BIN" "$(dirname "$LOG")"
  journal "---- $(sw_vers -productVersion) $(uname -m) · ${1:-installation}"

  # Lance via « curl | bash », l'entree standard est le script lui-meme :
  # les questions doivent lire le clavier.
  if ! { exec </dev/tty; } 2>/dev/null; then
    echo "Cet installateur pose des questions : lancez la commande dans l'application Terminal." >&2
    exit 1
  fi

  printf '\n  Préparation…\n'
  installer_gum
  clear

  case "${1:-}" in
    --desinstaller|--uninstall) desinstaller ;;
    *) installer ;;
  esac
}

# Sur une seule ligne : bash ne lit rien de plus si le telechargement est coupe.
main "$@"; exit
