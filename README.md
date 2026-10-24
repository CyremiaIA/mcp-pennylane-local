# Connecteur Pennylane pour Claude — version locale

Serveur MCP pour l'**API Pennylane v2**, installé **sur le Mac de l'utilisateur** :
lecture complète, plus l'écriture que le MCP officiel de Pennylane ne propose pas
(devis, produits, clients, rapprochement bancaire). 29 outils.

Rien n'est hébergé : Claude Desktop lance le serveur localement (stdio), qui parle
directement à Pennylane avec le token de l'utilisateur.

## Installation (macOS)

Dans le Terminal :

```bash
curl -fsSL https://raw.githubusercontent.com/CyremiaIA/mcp-pennylane-local/main/install.sh | bash
```

L'installateur guide pas à pas (interface [gum](https://github.com/charmbracelet/gum)) :

1. **Préparation** : vérifie que Claude est installé, télécharge gum et
   [uv](https://github.com/astral-sh/uv) (versions épinglées, empreintes SHA-256
   vérifiées), puis Python et les dépendances. Aucun droit administrateur.
2. **Token** : explique où le créer dans Pennylane, le vérifie (`GET /me`), signale
   un token en lecture seule, puis le range dans le **trousseau macOS**.
3. **Claude** : ferme Claude si besoin, ajoute l'entrée `pennylane` à
   `claude_desktop_config.json` sans toucher au reste (sauvegarde horodatée à côté),
   et à Claude Code s'il est présent.
4. **Test** : lance le serveur exactement comme Claude le fera, vérifie les 29 outils
   et interroge Pennylane.
5. **Fin** : rouvre Claude et propose des questions d'essai.

Relancer la commande est sans risque : elle met à jour le connecteur et propose de
garder le token déjà enregistré.

### Désinstallation

```bash
curl -fsSL https://raw.githubusercontent.com/CyremiaIA/mcp-pennylane-local/main/install.sh | bash -s -- --desinstaller
```

Retire l'entrée de Claude, le token du trousseau et tous les fichiers. Le token reste
à révoquer dans Pennylane.

## Où sont les choses

| Quoi | Où |
|---|---|
| Serveur, uv, gum, Python, cache | `~/Library/Application Support/Cyremia/pennylane-mcp/` |
| Token | Trousseau macOS, service `cyremia-pennylane-mcp` |
| Journal d'installation (sans le token) | `~/Library/Logs/Cyremia/installation-pennylane-mcp.log` |
| Journal du serveur (appels d'outils, écritures marquées `[ECRITURE]`) | `~/Library/Logs/Claude/mcp-server-pennylane.log` |

## Côté Pennylane

Paramètres → Connectivité → Développeurs → « Générer un token API », version
**API V2**, droits **Lecture et écriture**. Il faut un compte administrateur et une
offre Essential ou supérieure. Le token n'est affiché qu'une fois.

## Origine du code

`pennylane_mcp.py` reprend les outils de `CyremiaIA/mcp-pennylane` (commit `7d56e26`),
sans la partie serveur distant (HTTP, OAuth Google, jetons de service, sonde) :
uniquement stdio. Le token est lu dans `PENNYLANE_API_KEY`, sinon dans le trousseau.
Un correctif d'outil fait dans le dépôt d'origine est à reporter ici à la main.

## Tester une modification sans publier

```bash
MCP_PL_SOURCE="file://$PWD" bash install.sh
```
