# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastmcp==3.4.7",
#     "mcp==1.29.0",
#     "httpx==0.28.1",
#     "pydantic==2.13.4",
# ]
# ///
"""
Pennylane MCP Server — version locale (Cyremia)
Couverture de l'API Pennylane v2 : lecture complete + ecriture sur le catalogue
produits, les devis, les clients et le rapprochement bancaire.

Version LOCALE de CyremiaIA/mcp-pennylane : memes outils, mais uniquement en
stdio (lance par Claude sur le poste de l'utilisateur). Pas de serveur HTTP,
pas d'OAuth, rien n'est expose sur le reseau.

Raison d'etre : le MCP officiel fourni par Pennylane est en LECTURE SEULE.
Ce serveur apporte l'ecriture (devis, produits, clients, rapprochement) et les
ressources absentes de l'officiel (devis, abonnements, comptes bancaires).

Token API Pennylane, dans cet ordre :
  1. variable d'environnement PENNYLANE_API_KEY
  2. trousseau macOS (service KEYCHAIN_SERVICE), ou l'installateur le range

Conventions API v2 (verifiees contre la spec OpenAPI et l'API live) :
  - pagination par curseur : parametres `limit` (1..100, defaut API 20) + `cursor`
  - filtres : ?filter=[{"field":..,"operator":..,"value":..}] avec une liste de
    champs/operateurs autorisee PAR endpoint (un champ non autorise => HTTP 400)
  - aucun endpoint ne renvoie ses lignes en ligne : elles se lisent via la
    sous-ressource `/<resource>/{id}/invoice_lines`
"""

import os
import sys
import time
import json
import asyncio
import logging
import subprocess
from datetime import datetime
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware as FastMCPMiddleware

# --- Configuration ---

KEYCHAIN_SERVICE = "cyremia-pennylane-mcp"


def _read_api_key() -> str:
    """Token API : variable d'environnement, sinon trousseau macOS."""
    key = os.environ.get("PENNYLANE_API_KEY", "").strip()
    if key:
        return key
    try:
        r = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


API_KEY  = _read_api_key()
BASE_URL = "https://app.pennylane.com/api/external/v2"
TIMEOUT  = 30.0
MAX_LIMIT = 100
# Nombre max de pages parcourues pour une recherche texte cote client
# (l'API v2 n'expose pas d'operateur "contient").
SEARCH_MAX_PAGES = 10
# Nombre max de pages de lignes ramenees pour un document.
LINES_MAX_PAGES = 5

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

# En stdio, stdout porte le protocole MCP : les journaux vont sur stderr, que
# Claude Desktop recopie dans ~/Library/Logs/Claude/mcp-server-pennylane.log.
logging.basicConfig(
    level=LOG_LEVEL,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("pennylane-mcp")


mcp = FastMCP("Pennylane")


# --- Client HTTP async ---

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    }


async def pl_get(path: str, params: dict | None = None) -> dict:
    """GET vers l'API Pennylane v2."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await client.get(
                f"{BASE_URL}{path}",
                headers=_headers(),
                params=params or {},
            )
            if r.status_code >= 400:
                return {
                    "erreur": f"HTTP {r.status_code}",
                    "details": r.text[:500],
                    "path": path,
                }
            return r.json()
        except httpx.TimeoutException:
            return {"erreur": "Timeout", "path": path}
        except Exception as e:
            return {"erreur": str(e), "path": path}


async def pl_write(method: str, path: str, body: dict | None = None) -> dict:
    """POST / PUT / DELETE vers l'API Pennylane v2 (corps JSON)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await client.request(
                method,
                f"{BASE_URL}{path}",
                headers=_headers(),
                json=body if body is not None else None,
            )
            if r.status_code >= 400:
                return {
                    "erreur": f"HTTP {r.status_code}",
                    "details": r.text[:800],
                    "method": method,
                    "path": path,
                    "corps_envoye": body,
                }
            if r.status_code == 204 or not r.text.strip():
                return {"ok": True, "status": r.status_code}
            return r.json()
        except httpx.TimeoutException:
            return {"erreur": "Timeout", "method": method, "path": path}
        except Exception as e:
            return {"erreur": str(e), "method": method, "path": path}


# --- Helpers pagination / filtres ---

def _field(name: str, operator: str, value: Any) -> dict:
    return {"field": name, "operator": operator, "value": value}


def _page_params(limit: int, cursor: str, filters: list | None = None) -> dict:
    params: dict = {"limit": max(1, min(limit, MAX_LIMIT))}
    if cursor:
        params["cursor"] = cursor
    if filters:
        params["filter"] = json.dumps(filters)
    return params


def _raw_items(result: dict) -> list:
    items = result.get("items")
    return items if isinstance(items, list) else []


def _paged(result: dict, items: list) -> dict:
    return {
        "items":       items,
        "count":       len(items),
        "has_more":    bool(result.get("has_more")),
        "next_cursor": result.get("next_cursor") or "",
    }


async def _fetch_all(path: str, filters: list | None = None,
                     max_pages: int = SEARCH_MAX_PAGES) -> list | dict:
    """Parcourt la pagination curseur et renvoie les items bruts (ou une erreur)."""
    out: list = []
    cursor = ""
    for _ in range(max_pages):
        result = await pl_get(path, _page_params(MAX_LIMIT, cursor, filters))
        if "erreur" in result:
            return result
        out.extend(_raw_items(result))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor") or ""
        if not cursor:
            break
    return out


def _matches(obj: dict, needle: str, fields: tuple) -> bool:
    """Recherche sous-chaine insensible a la casse sur plusieurs champs."""
    n = needle.lower()
    for f in fields:
        v = obj.get(f)
        if isinstance(v, list):
            if any(n in str(x).lower() for x in v):
                return True
        elif v and n in str(v).lower():
            return True
    return False


def _addr(obj: dict, key: str) -> dict:
    a = obj.get(key)
    return a if isinstance(a, dict) else {}


def _ref_id(obj: dict, key: str) -> Any:
    """Extrait l'id d'une reference {id, url} renvoyee par l'API v2."""
    v = obj.get(key)
    return v.get("id", "") if isinstance(v, dict) else ""


def _plain(value: Any) -> str:
    """
    Aplatit le rich-text Pennylane en texte brut.
    L'UI stocke descriptions et textes libres au format Slate
    (`[{"type":"paragraph","children":[{"text":"..."}]}]`) ; le renvoyer tel quel
    pollue les reponses MCP.
    """
    if not isinstance(value, str) or not value.startswith("["):
        return value or ""
    try:
        nodes = json.loads(value)
    except Exception:
        return value

    def texte(node: Any) -> str:
        if isinstance(node, list):
            return "".join(texte(n) for n in node)
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                return node["text"]
            return texte(node.get("children"))
        return ""

    if not isinstance(nodes, list):
        return value
    # Un noeud de premier niveau = un paragraphe.
    return "\n".join(texte(n) for n in nodes).strip()


# --- Helpers lignes de document ---

def _line_out(l: dict) -> dict:
    return {
        "id":            l.get("id"),
        "label":         l.get("label", ""),
        "description":   _plain(l.get("description")),
        "quantite":      l.get("quantity", ""),
        "unite":         l.get("unit", ""),
        "prix_unitaire": l.get("raw_currency_unit_price", ""),
        "total_ht":      l.get("currency_amount_before_tax", ""),
        "total_ttc":     l.get("currency_amount", ""),
        "taux_tva":      l.get("vat_rate", ""),
        "remise":        l.get("discount") or {},
        "section_rank":  l.get("section_rank", ""),
        "product_id":    _ref_id(l, "product"),
    }


async def _fetch_lines(path: str) -> list:
    """
    Lit les lignes d'un document via sa sous-ressource /invoice_lines.
    Renvoie une liste normalisee ; en cas d'erreur, une liste contenant l'erreur.
    """
    raw = await _fetch_all(path, max_pages=LINES_MAX_PAGES)
    if isinstance(raw, dict):
        return [raw]
    return [_line_out(l) for l in raw]


# === 0. Informations societe ===

@mcp.tool(
    name="pennylane_company_info",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_company_info() -> dict:
    """
    Retourne les informations de la societe connectee (nom, SIREN, utilisateur)
    ainsi que la liste des scopes accordes au token API.
    Utile pour initialiser le contexte et verifier les droits d'ecriture
    (`products:all`, `quotes:all`) avant toute creation.
    """
    return await pl_get("/me")


# === 1. Clients ===

def _customer_out(c: dict) -> dict:
    addr = _addr(c, "billing_address")
    emails = c.get("emails") or []
    return {
        "id":                 c.get("id"),
        "nom":                c.get("name", ""),
        "type":               c.get("customer_type", ""),
        "emails":             emails,
        "telephone":          c.get("phone", ""),
        "siren":              c.get("reg_no", ""),
        "tva_intra":          c.get("vat_number", ""),
        "adresse":            addr.get("address", ""),
        "code_postal":        addr.get("postal_code", ""),
        "ville":              addr.get("city", ""),
        "pays":               addr.get("country_alpha2", ""),
        "reference":          c.get("reference", ""),
        "external_reference": c.get("external_reference", ""),
        "conditions_paiement": c.get("payment_conditions", ""),
    }


@mcp.tool(
    name="pennylane_list_customers",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_customers(
    search: str = "",
    limit: int = 50,
    cursor: str = "",
) -> dict:
    """
    Liste les clients (entreprises et particuliers).

    Args:
        search: Recherche texte libre (sous-chaine, insensible a la casse) sur le
                nom, la reference et les emails. L'API Pennylane v2 n'ayant pas
                d'operateur "contient" sur les clients, la recherche est faite
                cote serveur MCP en parcourant le catalogue clients ; dans ce mode
                la pagination par curseur ne s'applique pas.
        limit:  Resultats par page (1..100) quand `search` est vide.
        cursor: Curseur de pagination (next_cursor de la reponse precedente).

    Returns:
        items, count, has_more, next_cursor. Chaque item : id, nom, type, emails,
        telephone, siren, tva_intra, adresse, code_postal, ville, pays, reference.
        L'`id` est celui a passer en `customer_id` pour creer un devis.
    """
    if search:
        raw = await _fetch_all("/customers")
        if isinstance(raw, dict):
            return raw
        items = [_customer_out(c) for c in raw
                 if _matches(c, search, ("name", "reference", "external_reference", "emails"))]
        return {"items": items, "count": len(items), "has_more": False,
                "next_cursor": "", "recherche": "locale (sous-chaine)"}

    result = await pl_get("/customers", _page_params(limit, cursor))
    if "erreur" in result:
        return result
    return _paged(result, [_customer_out(c) for c in _raw_items(result)])


@mcp.tool(
    name="pennylane_get_customer",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_get_customer(customer_id: str) -> dict:
    """
    Retourne tous les details bruts d'un client.

    Args:
        customer_id: ID Pennylane du client (via pennylane_list_customers).
    """
    return await pl_get(f"/customers/{customer_id}")


@mcp.tool(
    name="pennylane_create_customer",
    annotations={"readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False},
)
async def pennylane_create_customer(
    customer_type: str,
    address: str,
    postal_code: str,
    city: str,
    country_alpha2: str = "FR",
    name: str = "",
    first_name: str = "",
    last_name: str = "",
    emails: list[str] | None = None,
    phone: str = "",
    vat_number: str = "",
    reg_no: str = "",
    reference: str = "",
    external_reference: str = "",
    payment_conditions: str = "",
    billing_language: str = "",
    recipient: str = "",
    notes: str = "",
) -> dict:
    """
    Cree un client dans Pennylane.

    ATTENTION : la lecture est unifiee (/customers renvoie les deux types) mais
    l'ecriture est scindee en deux endpoints selon le type :
      - entreprise  -> POST /company_customers    (exige `name`)
      - particulier -> POST /individual_customers (exige `first_name` + `last_name`)

    L'adresse de facturation est OBLIGATOIRE dans les deux cas, et ses quatre
    champs le sont aussi cote API : pas d'adresse partielle acceptee.

    Args:
        customer_type:  OBLIGATOIRE. "company" (entreprise) ou "individual"
                        (particulier). Determine l'endpoint et les champs requis.
        address:        OBLIGATOIRE. Rue et numero de l'adresse de facturation.
        postal_code:    OBLIGATOIRE. Code postal de facturation.
        city:           OBLIGATOIRE. Ville de facturation.
        country_alpha2: Code pays ISO 3166-1 alpha-2 (2 lettres). Defaut "FR".
        name:           Raison sociale. OBLIGATOIRE si customer_type="company",
                        ignore sinon.
        first_name:     Prenom. OBLIGATOIRE si customer_type="individual".
        last_name:      Nom. OBLIGATOIRE si customer_type="individual".
        emails:         Liste d'adresses e-mail, ex. ["contact@client.fr"].
                        L'API v2 attend des chaines, pas des objets.
        phone:          Telephone.
        vat_number:     Numero de TVA intracommunautaire (entreprises).
        reg_no:         Numero d'immatriculation / SIREN (entreprises).
        reference:      Code reference interne.
        external_reference: Identifiant unique externe (genere si vide).
        payment_conditions: Conditions de paiement. Defaut API : "30_days".
        billing_language:   Langue de facturation (ex. "fr_FR").
        recipient:      Destinataire / interlocuteur pour la facturation.
        notes:          Notes libres.

    Returns:
        Le client cree normalise (id, nom, type, emails, adresse, ...), ou
        {"erreur": ...} en cas d'echec. L'`id` retourne est celui a passer en
        `customer_id` pour creer un devis.
    """
    t = (customer_type or "").strip().lower()
    if t in ("entreprise", "societe", "company"):
        t = "company"
    elif t in ("particulier", "individu", "individual"):
        t = "individual"
    else:
        return {"erreur": "customer_type invalide",
                "detail": "Attendu 'company' (entreprise) ou 'individual' "
                          f"(particulier), recu '{customer_type}'."}

    manquants = [n for n, v in (("address", address), ("postal_code", postal_code),
                                ("city", city), ("country_alpha2", country_alpha2)) if not v]
    if manquants:
        return {"erreur": "adresse de facturation incomplete",
                "detail": "L'API v2 exige les quatre champs. Manquant(s) : "
                          + ", ".join(manquants)}

    if t == "company" and not name:
        return {"erreur": "name est obligatoire pour un client entreprise"}
    if t == "individual" and not (first_name and last_name):
        return {"erreur": "first_name et last_name sont obligatoires pour un "
                          "client particulier"}

    body: dict = {
        "billing_address": {
            "address":        address,
            "postal_code":    postal_code,
            "city":           city,
            "country_alpha2": country_alpha2.upper(),
        },
    }
    if t == "company":
        body["name"] = name
        path = "/company_customers"
    else:
        body["first_name"] = first_name
        body["last_name"] = last_name
        path = "/individual_customers"

    if emails:             body["emails"] = list(emails)
    if phone:              body["phone"] = phone
    # vat_number / reg_no n'existent que sur le schema entreprise : les envoyer
    # sur /individual_customers ferait echouer la requete en 400.
    if t == "company":
        if vat_number:     body["vat_number"] = vat_number
        if reg_no:         body["reg_no"] = reg_no
    if reference:          body["reference"] = reference
    if external_reference: body["external_reference"] = external_reference
    if payment_conditions: body["payment_conditions"] = payment_conditions
    if billing_language:   body["billing_language"] = billing_language
    if recipient:          body["recipient"] = recipient
    if notes:              body["notes"] = notes

    result = await pl_write("POST", path, body)
    if "erreur" in result:
        return result
    return _customer_out(result)


# === 2. Factures clients ===

@mcp.tool(
    name="pennylane_list_customer_invoices",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_customer_invoices(
    customer_id: str = "",
    quote_id: str = "",
    draft: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
    cursor: str = "",
) -> dict:
    """
    Liste les factures clients emises par la societe.

    Args:
        customer_id: Filtrer par client (ID Pennylane).
        quote_id:    Filtrer les factures issues d'un devis donne.
        draft:       "true" = brouillons uniquement, "false" = finalisees uniquement.
        date_from:   Date minimale d'emission (YYYY-MM-DD).
        date_to:     Date maximale d'emission (YYYY-MM-DD).
        limit:       Resultats par page (1..100).
        cursor:      Curseur de pagination (next_cursor de la reponse precedente).

    Note: l'API v2 n'autorise PAS de filtre serveur sur `status` pour cet endpoint
    (champs autorises : id, date, customer_id, billing_subscription_id, quote_id,
    invoice_number, draft, external_reference, flow_id). Le statut est renvoye dans
    chaque item, a filtrer cote appelant.

    Returns:
        items avec id, numero, client_id, date, echeance, montant_ht, montant_ttc,
        taxe, reste_du, statut, paye, brouillon, label, currency, quote_id.
    """
    filters = []
    if customer_id:
        filters.append(_field("customer_id", "eq", customer_id))
    if quote_id:
        filters.append(_field("quote_id", "eq", quote_id))
    if draft:
        filters.append(_field("draft", "eq", draft.strip().lower() in ("1", "true", "oui", "yes")))
    if date_from:
        filters.append(_field("date", "gteq", date_from))
    if date_to:
        filters.append(_field("date", "lteq", date_to))

    result = await pl_get("/customer_invoices", _page_params(limit, cursor, filters))
    if "erreur" in result:
        return result

    items = [
        {
            "id":          f.get("id"),
            "numero":      f.get("invoice_number", ""),
            "client_id":   _ref_id(f, "customer"),
            "date":        f.get("date", ""),
            "echeance":    f.get("deadline", ""),
            "montant_ht":  f.get("currency_amount_before_tax", ""),
            "montant_ttc": f.get("currency_amount", ""),
            "taxe":        f.get("currency_tax", ""),
            "reste_du":    f.get("remaining_amount_with_tax", ""),
            "statut":      f.get("status", ""),
            "paye":        f.get("paid", False),
            "brouillon":   f.get("draft", False),
            "label":       f.get("label", ""),
            "currency":    f.get("currency", "EUR"),
            "quote_id":    _ref_id(f, "quote"),
        }
        for f in _raw_items(result)
    ]
    return _paged(result, items)


@mcp.tool(
    name="pennylane_get_customer_invoice",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_get_customer_invoice(invoice_id: str) -> dict:
    """
    Retourne les details complets d'une facture client avec toutes ses lignes.
    (Deux appels API : la facture, puis sa sous-ressource /invoice_lines.)

    Args:
        invoice_id: ID Pennylane de la facture (via pennylane_list_customer_invoices).
    """
    f = await pl_get(f"/customer_invoices/{invoice_id}")
    if "erreur" in f:
        return f

    lignes = await _fetch_lines(f"/customer_invoices/{invoice_id}/invoice_lines")
    return {
        "id":          f.get("id"),
        "numero":      f.get("invoice_number", ""),
        "client_id":   _ref_id(f, "customer"),
        "date":        f.get("date", ""),
        "echeance":    f.get("deadline", ""),
        "statut":      f.get("status", ""),
        "paye":        f.get("paid", False),
        "brouillon":   f.get("draft", False),
        "montant_ht":  f.get("currency_amount_before_tax", ""),
        "montant_ttc": f.get("currency_amount", ""),
        "currency":    f.get("currency", "EUR"),
        "objet":       _plain(f.get("pdf_invoice_subject")),
        "description": _plain(f.get("pdf_description")),
        "pdf_url":     f.get("public_file_url", ""),
        "lignes":      lignes,
    }


# === 3. Factures fournisseurs ===

PAYMENT_STATUSES = (
    "to_be_processed", "to_be_paid", "partially_paid", "payment_error",
    "payment_scheduled", "payment_in_progress", "payment_emitted",
    "payment_found", "paid_offline", "fully_paid",
)


@mcp.tool(
    name="pennylane_list_supplier_invoices",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_supplier_invoices(
    supplier_id: str = "",
    payment_status: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
    cursor: str = "",
) -> dict:
    """
    Liste les factures fournisseurs (achats de la societe).

    Args:
        supplier_id:    Filtrer par fournisseur (ID Pennylane, via
                        pennylane_list_suppliers — le filtre par NOM n'existe pas
                        cote API).
        payment_status: to_be_processed | to_be_paid | partially_paid |
                        payment_error | payment_scheduled | payment_in_progress |
                        payment_emitted | payment_found | paid_offline | fully_paid.
        date_from:      Date minimale (YYYY-MM-DD).
        date_to:        Date maximale (YYYY-MM-DD).
        limit:          Resultats par page (1..100).
        cursor:         Curseur de pagination.
    """
    if payment_status and payment_status not in PAYMENT_STATUSES:
        return {"erreur": f"payment_status invalide: {payment_status}",
                "valeurs_autorisees": list(PAYMENT_STATUSES)}

    filters = []
    if supplier_id:
        filters.append(_field("supplier_id", "eq", supplier_id))
    if payment_status:
        filters.append(_field("payment_status", "eq", payment_status))
    if date_from:
        filters.append(_field("date", "gteq", date_from))
    if date_to:
        filters.append(_field("date", "lteq", date_to))

    result = await pl_get("/supplier_invoices", _page_params(limit, cursor, filters))
    if "erreur" in result:
        return result

    items = [
        {
            "id":             f.get("id"),
            "numero":         f.get("invoice_number", ""),
            "fournisseur_id": _ref_id(f, "supplier"),
            "date":           f.get("date", ""),
            "echeance":       f.get("deadline", ""),
            "montant_ht":     f.get("currency_amount_before_tax", ""),
            "montant_ttc":    f.get("currency_amount", ""),
            "taxe":           f.get("currency_tax", ""),
            "statut_paiement": f.get("payment_status", ""),
            "paye":           f.get("paid", False),
            "label":          f.get("label", ""),
            "currency":       f.get("currency", "EUR"),
        }
        for f in _raw_items(result)
    ]
    return _paged(result, items)


@mcp.tool(
    name="pennylane_get_supplier_invoice",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_get_supplier_invoice(invoice_id: str) -> dict:
    """
    Retourne les lignes detaillees d'une facture fournisseur.

    Args:
        invoice_id: ID Pennylane (via pennylane_list_supplier_invoices).
    """
    f = await pl_get(f"/supplier_invoices/{invoice_id}")
    if "erreur" in f:
        return f

    lignes = await _fetch_lines(f"/supplier_invoices/{invoice_id}/invoice_lines")
    return {
        "id":              f.get("id"),
        "numero":          f.get("invoice_number", ""),
        "fournisseur_id":  _ref_id(f, "supplier"),
        "date":            f.get("date", ""),
        "echeance":        f.get("deadline", ""),
        "statut_paiement": f.get("payment_status", ""),
        "montant_ht":      f.get("currency_amount_before_tax", ""),
        "montant_ttc":     f.get("currency_amount", ""),
        "currency":        f.get("currency", "EUR"),
        "pdf_url":         f.get("public_file_url", ""),
        "lignes":          lignes,
    }


# === 4. Fournisseurs ===

def _supplier_out(s: dict) -> dict:
    addr = _addr(s, "postal_address")
    return {
        "id":                 s.get("id"),
        "nom":                s.get("name", ""),
        "emails":             s.get("emails") or [],
        "siren":              s.get("reg_no", ""),
        "tva_intra":          s.get("vat_number", ""),
        "iban":               s.get("iban", ""),
        "adresse":            addr.get("address", ""),
        "code_postal":        addr.get("postal_code", ""),
        "ville":              addr.get("city", ""),
        "pays":               addr.get("country_alpha2", ""),
        "external_reference": s.get("external_reference", ""),
    }


@mcp.tool(
    name="pennylane_list_suppliers",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_suppliers(
    search: str = "",
    limit: int = 50,
    cursor: str = "",
) -> dict:
    """
    Liste les fournisseurs enregistres dans Pennylane.

    Args:
        search: Recherche texte libre (sous-chaine) sur le nom et les emails,
                effectuee cote serveur MCP (l'API v2 ne propose que `start_with`).
                Dans ce mode la pagination par curseur ne s'applique pas.
        limit:  Resultats par page (1..100) quand `search` est vide.
        cursor: Curseur de pagination.
    """
    if search:
        raw = await _fetch_all("/suppliers")
        if isinstance(raw, dict):
            return raw
        items = [_supplier_out(s) for s in raw
                 if _matches(s, search, ("name", "emails", "external_reference"))]
        return {"items": items, "count": len(items), "has_more": False,
                "next_cursor": "", "recherche": "locale (sous-chaine)"}

    result = await pl_get("/suppliers", _page_params(limit, cursor))
    if "erreur" in result:
        return result
    return _paged(result, [_supplier_out(s) for s in _raw_items(result)])


@mcp.tool(
    name="pennylane_get_supplier",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_get_supplier(supplier_id: str) -> dict:
    """
    Retourne les details complets d'un fournisseur.

    Args:
        supplier_id: ID Pennylane du fournisseur.
    """
    return await pl_get(f"/suppliers/{supplier_id}")


# === 5. Produits / catalogue ===

# Codes de TVA francais les plus courants (l'enum complet compte 173 valeurs,
# prefixees par pays : FR_*, BE_*, DE_*, ... plus `exempt`, `intracom_*`,
# `extracom`, `crossborder`, `mixed`).
VAT_RATES_FR = {
    "FR_200": "20 %",
    "FR_100": "10 %",
    "FR_55":  "5,5 %",
    "FR_21":  "2,1 %",
    "exempt": "exonere / 0 %",
    "FR_85":  "8,5 % (DOM)",
    "FR_09":  "0,9 %",
    "FR_130": "13 %",
    "FR_200_construction": "20 % autoliquidation batiment",
    "FR_100_construction": "10 % autoliquidation batiment",
}


def _product_out(p: dict) -> dict:
    """Normalise un produit Pennylane v2 pour la sortie MCP."""
    return {
        "id":                 p.get("id"),
        "label":              p.get("label", ""),
        "description":        p.get("description", ""),
        "reference":          p.get("reference", ""),
        "external_reference": p.get("external_reference", ""),
        "prix_ht":            p.get("price_before_tax", ""),
        "prix_ttc":           p.get("price", ""),
        "taux_tva":           p.get("vat_rate", ""),
        "unite":              p.get("unit", ""),
        "currency":           p.get("currency", "EUR"),
        "ledger_account_id":  _ref_id(p, "ledger_account"),
        "archived_at":        p.get("archived_at"),
    }


@mcp.tool(
    name="pennylane_list_products",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_products(
    search: str = "",
    reference: str = "",
    limit: int = 50,
    cursor: str = "",
) -> dict:
    """
    Liste les produits/services du catalogue Pennylane.

    Args:
        search:    Recherche texte libre (sous-chaine, insensible a la casse) sur
                   le label, la description et la reference. L'API v2 n'acceptant
                   que l'egalite stricte sur `label`/`reference`, la recherche est
                   faite cote serveur MCP en parcourant le catalogue ; dans ce mode
                   la pagination par curseur ne s'applique pas.
        reference: Reference produit EXACTE (filtre serveur, plus rapide).
        limit:     Resultats par page (1..100) quand `search` est vide.
        cursor:    Curseur de pagination.

    Returns:
        items avec id, label, description, reference, prix_ht, prix_ttc, taux_tva,
        unite, currency, archived_at. L'`id` est celui a passer en `product_id`
        dans les lignes de devis.
    """
    if search:
        raw = await _fetch_all("/products")
        if isinstance(raw, dict):
            return raw
        items = [_product_out(p) for p in raw
                 if _matches(p, search, ("label", "description", "reference",
                                         "external_reference"))]
        return {"items": items, "count": len(items), "has_more": False,
                "next_cursor": "", "recherche": "locale (sous-chaine)"}

    filters = [_field("reference", "eq", reference)] if reference else None
    result = await pl_get("/products", _page_params(limit, cursor, filters))
    if "erreur" in result:
        return result
    return _paged(result, [_product_out(p) for p in _raw_items(result)])


@mcp.tool(
    name="pennylane_get_product",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_get_product(product_id: str) -> dict:
    """
    Retourne les details d'un produit/service du catalogue.

    Args:
        product_id: ID Pennylane du produit.
    """
    result = await pl_get(f"/products/{product_id}")
    if "erreur" in result:
        return result
    return _product_out(result)


@mcp.tool(
    name="pennylane_create_product",
    annotations={"readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False},
)
async def pennylane_create_product(
    label: str,
    price_before_tax: str,
    vat_rate: str = "FR_200",
    description: str = "",
    unit: str = "",
    currency: str = "EUR",
    reference: str = "",
    external_reference: str = "",
    ledger_account_id: int = 0,
) -> dict:
    """
    Cree un produit/service dans le catalogue Pennylane (POST /products).

    Args:
        label:            OBLIGATOIRE. Libelle du produit.
        price_before_tax: OBLIGATOIRE. Prix HORS TAXES sous forme de chaine
                          (ex: "150.00"). Doit matcher ^-?\\d+(\\.\\d+)?$ :
                          point decimal, pas de virgule, pas de symbole monetaire.
        vat_rate:         Code de taux de TVA. Defaut "FR_200" (20 %). Codes FR
                          courants : FR_200 (20 %), FR_100 (10 %), FR_55 (5,5 %),
                          FR_21 (2,1 %), "exempt" (0 %). ATTENTION : le code est
                          "FR_55" et non "FR_055", "FR_21" et non "FR_021".
        description:      Description detaillee (max 5000 caracteres).
        unit:             Unite de mesure libre (ex: "piece", "heure", "mois").
        currency:         Devise ISO 4217. Defaut "EUR".
        reference:        Code reference interne du produit.
        external_reference: Identifiant unique externe (genere si vide).
        ledger_account_id: ID du compte comptable associe (0 = ignorer).

    Returns:
        Le produit cree normalise (id, label, prix_ht, prix_ttc, taux_tva, ...),
        ou {"erreur": ...} en cas d'echec.
    """
    body: dict = {
        "label":            label,
        "price_before_tax": str(price_before_tax).replace(",", "."),
        "vat_rate":         vat_rate,
    }
    if description:        body["description"] = description
    if unit:               body["unit"] = unit
    if currency:           body["currency"] = currency
    if reference:          body["reference"] = reference
    if external_reference: body["external_reference"] = external_reference
    if ledger_account_id:  body["ledger_account_id"] = ledger_account_id

    result = await pl_write("POST", "/products", body)
    if "erreur" in result:
        return result
    return _product_out(result)


@mcp.tool(
    name="pennylane_update_product",
    annotations={"readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_update_product(
    product_id: str,
    label: str = "",
    price_before_tax: str = "",
    vat_rate: str = "",
    description: str = "",
    unit: str = "",
    currency: str = "",
    reference: str = "",
    external_reference: str = "",
    ledger_account_id: int = 0,
) -> dict:
    """
    Modifie un produit existant (PUT /products/{id}).
    Seuls les champs fournis (non vides) sont envoyes ; les autres restent inchanges.

    Args:
        product_id:       OBLIGATOIRE. ID Pennylane du produit a modifier.
        label:            Nouveau libelle.
        price_before_tax: Nouveau prix HT (chaine, ex: "199.90").
        vat_rate:         Nouveau code de TVA (ex: "FR_200", "FR_100", "FR_55").
        description:      Nouvelle description (max 5000 caracteres).
        unit:             Nouvelle unite.
        currency:         Nouvelle devise.
        reference:        Nouvelle reference interne.
        external_reference: Nouvel identifiant externe.
        ledger_account_id: Nouveau compte comptable (0 = ne pas modifier).

    Note: l'API v2 n'expose ni suppression ni archivage de produit
    (`archived_at` est en lecture seule). Pour retirer un produit du catalogue,
    passer par l'interface Pennylane.

    Returns:
        Le produit mis a jour normalise, ou {"erreur": ...}.
    """
    body: dict = {}
    if label:              body["label"] = label
    if price_before_tax:   body["price_before_tax"] = str(price_before_tax).replace(",", ".")
    if vat_rate:           body["vat_rate"] = vat_rate
    if description:        body["description"] = description
    if unit:               body["unit"] = unit
    if currency:           body["currency"] = currency
    if reference:          body["reference"] = reference
    if external_reference: body["external_reference"] = external_reference
    if ledger_account_id:  body["ledger_account_id"] = ledger_account_id

    if not body:
        return {"erreur": "Aucun champ a modifier fourni.", "product_id": product_id}

    result = await pl_write("PUT", f"/products/{product_id}", body)
    if "erreur" in result:
        return result
    return _product_out(result)


# === 6. Devis ===

QUOTE_STATUSES = ("pending", "accepted", "denied", "invoiced", "expired")
QUOTE_LANGUAGES = ("fr_FR", "en_GB", "de_DE")

# Alias FR acceptes dans les lignes de devis, pour tolerer les appels naturels.
_LINE_ALIASES = {
    "libelle":       "label",
    "quantite":      "quantity",
    "prix_unitaire": "raw_currency_unit_price",
    "prix_ht":       "raw_currency_unit_price",
    "unit_price":    "raw_currency_unit_price",
    "unite":         "unit",
    "taux_tva":      "vat_rate",
    "tva":           "vat_rate",
    "remise":        "discount",
    "produit_id":    "product_id",
}

_LINE_KEYS_CREATE = {
    "product_id", "quantity", "label", "ledger_account_id",
    "raw_currency_unit_price", "unit", "vat_rate", "description",
    "section_rank", "discount",
}
_LINE_KEYS_UPDATE = _LINE_KEYS_CREATE | {"id"}


def _as_lines(value: Any, name: str) -> list | dict:
    """Accepte une liste de dicts ou une chaine JSON ; renvoie une liste ou une erreur."""
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception as e:
            return {"erreur": f"{name} n'est pas un JSON valide: {e}"}
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return {"erreur": f"{name} doit etre une liste d'objets."}
    for l in value:
        if not isinstance(l, dict):
            return {"erreur": f"{name}: chaque element doit etre un objet, recu {type(l).__name__}."}
    return value


def _clean_discount(d: Any) -> dict | None:
    if not isinstance(d, dict):
        return None
    t = d.get("type") or d.get("mode")
    v = d.get("value") or d.get("valeur")
    if t not in ("absolute", "relative") or v in (None, ""):
        return None
    return {"type": t, "value": str(v).replace(",", ".")}


def _norm_line(line: dict, allowed: set, mode: str) -> dict:
    """
    Normalise une ligne de devis : alias FR -> champs API, coercition de types,
    filtrage des cles inconnues (l'API refuse toute propriete supplementaire).
    Renvoie {"erreur": ...} si la ligne est invalide.
    """
    out: dict = {}
    for k, v in line.items():
        key = _LINE_ALIASES.get(k, k)
        if key not in allowed or v in (None, ""):
            continue
        out[key] = v

    for k in ("product_id", "ledger_account_id", "section_rank", "id"):
        if k in out:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError):
                return {"erreur": f"Ligne: `{k}` doit etre un entier, recu {out[k]!r}."}
    if "quantity" in out:
        try:
            out["quantity"] = float(str(out["quantity"]).replace(",", "."))
        except (TypeError, ValueError):
            return {"erreur": f"Ligne: `quantity` doit etre un nombre, recu {out['quantity']!r}."}
    if "raw_currency_unit_price" in out:
        out["raw_currency_unit_price"] = str(out["raw_currency_unit_price"]).replace(",", ".")
    if "discount" in out:
        d = _clean_discount(out["discount"])
        if d is None:
            out.pop("discount")
        else:
            out["discount"] = d

    if mode == "create":
        if "quantity" not in out:
            return {"erreur": "Ligne: `quantity` est obligatoire.", "ligne": line}
        if "product_id" not in out:
            manquants = [k for k in ("label", "raw_currency_unit_price", "unit", "vat_rate")
                         if k not in out]
            if manquants:
                return {
                    "erreur": "Ligne libre incomplete: sans `product_id`, les champs "
                              "label, raw_currency_unit_price, unit et vat_rate sont "
                              "tous obligatoires.",
                    "manquants": manquants,
                    "ligne": line,
                }
    elif mode == "update" and "id" not in out:
        return {"erreur": "Ligne a modifier: `id` est obligatoire.", "ligne": line}

    return out


def _norm_lines(lines: list, allowed: set, mode: str) -> list | dict:
    out = []
    for l in lines:
        n = _norm_line(l, allowed, mode)
        if "erreur" in n:
            return n
        out.append(n)
    return out


_SECTION_ALIASES = {
    "titre":       "title",
    "intitule":    "title",
    "rang":        "rank",
    "ordre":       "rank",
}
_SECTION_KEYS = {"title", "description", "rank"}


def _norm_sections(sections: list) -> list | dict:
    """
    Normalise les sections de lignes d'un devis (`invoice_line_sections`).

    Rappel API v2 : les sections ne se definissent QU'A LA CREATION du devis.
    `PUT /quotes/{id}` ne les accepte pas et il n'existe pas d'endpoint dedie
    (la sous-ressource /invoice_line_sections est en lecture seule).
    """
    out = []
    rangs = []
    for s in sections:
        n: dict = {}
        for k, v in s.items():
            key = _SECTION_ALIASES.get(k, k)
            if key not in _SECTION_KEYS or v in (None, ""):
                continue
            n[key] = v
        if "rank" not in n:
            return {"erreur": "Section: `rank` est obligatoire.", "section": s}
        try:
            n["rank"] = int(n["rank"])
        except (TypeError, ValueError):
            return {"erreur": f"Section: `rank` doit etre un entier, recu {n['rank']!r}."}
        if n["rank"] < 1:
            return {"erreur": "Section: `rank` commence a 1.", "section": s}
        if not n.get("title"):
            return {"erreur": "Section: `title` est obligatoire.", "section": s}
        rangs.append(n["rank"])
        out.append(n)

    doublons = {r for r in rangs if rangs.count(r) > 1}
    if doublons:
        return {"erreur": "Section: `rank` doit etre unique.",
                "rangs_en_double": sorted(doublons)}
    return out


def _check_section_ranks(lines: list, sections: list) -> dict | None:
    """
    Verifie que chaque `section_rank` de ligne pointe vers une section declaree.

    Sans ce garde-fou, l'API repond un 400 opaque (ou range la ligne hors
    section) : le rang doit correspondre au `rank` d'une section du meme devis.
    """
    connus = {s["rank"] for s in sections}
    utilises = {l["section_rank"] for l in lines if "section_rank" in l}
    orphelins = sorted(utilises - connus - {0})
    if orphelins:
        return {"erreur": "section_rank sans section correspondante",
                "detail": "Chaque `section_rank` de ligne doit correspondre au "
                          "`rank` d'une section passee dans `sections`.",
                "rangs_orphelins": orphelins,
                "rangs_declares": sorted(connus) or "aucune section declaree"}
    return None


def _quote_body(
    customer_id: int, date: str, deadline: str,
    subject: str, description: str, free_text: str, special_mention: str,
    currency: str, language: str, external_reference: str,
    quote_template_id: int, discount_type: str, discount_value: str,
) -> dict:
    body: dict = {}
    if customer_id:        body["customer_id"] = int(customer_id)
    if date:               body["date"] = date
    if deadline:           body["deadline"] = deadline
    if subject:            body["pdf_invoice_subject"] = subject
    if description:        body["pdf_description"] = description
    if free_text:          body["pdf_invoice_free_text"] = free_text
    if special_mention:    body["special_mention"] = special_mention
    if currency:           body["currency"] = currency
    if language:           body["language"] = language
    if external_reference: body["external_reference"] = external_reference
    if quote_template_id:  body["quote_template_id"] = int(quote_template_id)
    d = _clean_discount({"type": discount_type, "value": discount_value})
    if d:                  body["discount"] = d
    return body


def _quote_out(q: dict) -> dict:
    return {
        "id":                 q.get("id"),
        "numero":             q.get("quote_number", ""),
        "label":              q.get("label", ""),
        "client_id":          _ref_id(q, "customer"),
        "date":               q.get("date", ""),
        "echeance":           q.get("deadline", ""),
        "statut":             q.get("status", ""),
        "montant_ht":         q.get("currency_amount_before_tax", ""),
        "montant_ttc":        q.get("currency_amount", ""),
        "taxe":               q.get("currency_tax", ""),
        "currency":           q.get("currency", "EUR"),
        "langue":             q.get("language", ""),
        "objet":              _plain(q.get("pdf_invoice_subject")),
        "remise":             q.get("discount") or {},
        "external_reference": q.get("external_reference", ""),
        "pdf_url":            q.get("public_file_url", ""),
        "archived_at":        q.get("archived_at"),
    }


@mcp.tool(
    name="pennylane_list_quotes",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_quotes(
    customer_id: str = "",
    status: str = "",
    limit: int = 50,
    cursor: str = "",
) -> dict:
    """
    Liste les devis (propositions commerciales), du plus recent au plus ancien.

    Args:
        customer_id: Filtrer par client (ID Pennylane).
        status:      pending | accepted | denied | invoiced | expired.
        limit:       Resultats par page (1..100).
        cursor:      Curseur de pagination.

    Note: l'API v2 n'autorise le filtre que sur id, customer_id et status pour
    cet endpoint — pas de filtre serveur par date.
    """
    if status and status not in QUOTE_STATUSES:
        return {"erreur": f"status invalide: {status}",
                "valeurs_autorisees": list(QUOTE_STATUSES)}

    filters = []
    if customer_id:
        filters.append(_field("customer_id", "eq", customer_id))
    if status:
        filters.append(_field("status", "eq", status))

    result = await pl_get("/quotes", _page_params(limit, cursor, filters))
    if "erreur" in result:
        return result
    return _paged(result, [_quote_out(q) for q in _raw_items(result)])


@mcp.tool(
    name="pennylane_get_quote",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_get_quote(quote_id: str) -> dict:
    """
    Retourne les details complets d'un devis AVEC ses lignes.
    (Deux appels API : le devis, puis sa sous-ressource /invoice_lines.)

    Chaque ligne porte son `id` : c'est cet identifiant qu'il faut passer dans
    `lines_update` / `lines_delete` de pennylane_update_quote.

    Args:
        quote_id: ID Pennylane du devis (via pennylane_list_quotes).
    """
    q = await pl_get(f"/quotes/{quote_id}")
    if "erreur" in q:
        return q

    out = _quote_out(q)
    out["description"]      = _plain(q.get("pdf_description"))
    out["texte_libre"]      = _plain(q.get("pdf_invoice_free_text"))
    out["mention_speciale"] = _plain(q.get("special_mention"))
    out["lignes"] = await _fetch_lines(f"/quotes/{quote_id}/invoice_lines")
    return out


@mcp.tool(
    name="pennylane_create_quote",
    annotations={"readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False},
)
async def pennylane_create_quote(
    customer_id: int,
    date: str,
    deadline: str,
    lines: list[dict[str, Any]],
    subject: str = "",
    description: str = "",
    free_text: str = "",
    special_mention: str = "",
    currency: str = "EUR",
    language: str = "",
    external_reference: str = "",
    quote_template_id: int = 0,
    discount_type: str = "",
    discount_value: str = "",
    sections: list[Any] | None = None,
) -> dict:
    """
    Cree un devis dans Pennylane (POST /quotes).

    Args:
        customer_id: OBLIGATOIRE. ID Pennylane du client (via
                     pennylane_list_customers).
        date:        OBLIGATOIRE. Date d'emission du devis (YYYY-MM-DD).
        deadline:    OBLIGATOIRE. Date de fin de validite (YYYY-MM-DD).
        lines:       OBLIGATOIRE. Liste d'objets JSON (au moins un). Deux formes :

                     1) Ligne catalogue — reprend automatiquement le libelle, le
                        prix, l'unite, la TVA et le compte comptable du produit :
                        {"product_id": 94065930240, "quantity": 2}
                        Tout champ ajoute ecrase la valeur du produit
                        (ex. "raw_currency_unit_price": "899.00").

                     2) Ligne libre — sans produit du catalogue, ces cinq champs
                        sont TOUS obligatoires :
                        {"label": "Prestation d'installation",
                         "quantity": 1,
                         "raw_currency_unit_price": "450.00",
                         "unit": "piece",
                         "vat_rate": "FR_200"}

                     Champs optionnels communs : "description", "section_rank",
                     "ledger_account_id", "discount"
                     ({"type": "relative"|"absolute", "value": "10"}).
                     Les prix sont des CHAINES avec un point decimal.
                     Les alias francais (quantite, prix_unitaire, unite,
                     taux_tva, libelle, remise) sont acceptes.

        subject:            Objet/titre du devis affiche sur le PDF.
        description:        Description affichee sur le PDF.
        free_text:          Texte libre bas de page (ex: coordonnees du contact).
        special_mention:    Mentions particulieres.
        currency:           Devise ISO 4217. Defaut "EUR".
        language:           fr_FR | en_GB | de_DE (defaut : langue du client).
        external_reference: Reference externe unique (generee si vide).
        quote_template_id:  ID d'un modele de devis Pennylane (0 = defaut).
        discount_type:      Remise globale : "absolute" (montant) ou "relative" (%).
        discount_value:     Valeur de la remise globale (chaine, ex: "10").
        sections:           Sections de regroupement des lignes, affichees comme
                            des intertitres sur le PDF. Liste d'objets :
                            [{"title": "Materiel", "rank": 1},
                             {"title": "Prestations", "rank": 2,
                              "description": "Mise en service"}]
                            `title` et `rank` sont obligatoires, `rank` commence
                            a 1 et doit etre unique. Alias FR acceptes : titre,
                            rang. Pour ranger une ligne dans une section, mettre
                            son "section_rank" egal au "rank" de la section.

                            ATTENTION : l'API v2 ne permet de definir les
                            sections QU'A LA CREATION. `pennylane_update_quote`
                            ne peut ni en ajouter, ni en renommer, ni en
                            supprimer, et aucun endpoint dedie n'existe. La
                            structure en sections est donc figee des la creation.

    Returns:
        Le devis cree normalise (id, numero, statut, montants, pdf_url...),
        ou {"erreur": ..., "details": ...} avec la reponse brute de l'API.
    """
    parsed = _as_lines(lines, "lines")
    if isinstance(parsed, dict):
        return parsed
    if not parsed:
        return {"erreur": "`lines` est obligatoire : un devis doit contenir au moins une ligne."}

    normalized = _norm_lines(parsed, _LINE_KEYS_CREATE, "create")
    if isinstance(normalized, dict):
        return normalized

    if language and language not in QUOTE_LANGUAGES:
        return {"erreur": f"language invalide: {language}",
                "valeurs_autorisees": list(QUOTE_LANGUAGES)}
    for champ, valeur in (("date", date), ("deadline", deadline)):
        if not valeur:
            return {"erreur": f"`{champ}` est obligatoire (format YYYY-MM-DD)."}

    parsed_sections = _as_lines(sections, "sections")
    if isinstance(parsed_sections, dict):
        return parsed_sections
    norm_sections = _norm_sections(parsed_sections)
    if isinstance(norm_sections, dict):
        return norm_sections

    # Garde-fou : un section_rank qui ne pointe sur rien donne un 400 opaque.
    probleme = _check_section_ranks(normalized, norm_sections)
    if probleme:
        return probleme

    body = _quote_body(customer_id, date, deadline, subject, description,
                       free_text, special_mention, currency, language,
                       external_reference, quote_template_id,
                       discount_type, discount_value)
    body["invoice_lines"] = normalized
    if norm_sections:
        body["invoice_line_sections"] = norm_sections

    result = await pl_write("POST", "/quotes", body)
    if "erreur" in result:
        return result
    return _quote_out(result)


@mcp.tool(
    name="pennylane_update_quote",
    annotations={"readOnlyHint": False, "destructiveHint": True,
                 "idempotentHint": False, "openWorldHint": False},
)
async def pennylane_update_quote(
    quote_id: str,
    customer_id: int = 0,
    date: str = "",
    deadline: str = "",
    lines_create: list[dict[str, Any]] | None = None,
    lines_update: list[dict[str, Any]] | None = None,
    lines_delete: list[Any] | None = None,
    subject: str = "",
    description: str = "",
    free_text: str = "",
    special_mention: str = "",
    currency: str = "",
    language: str = "",
    external_reference: str = "",
    quote_template_id: int = 0,
    discount_type: str = "",
    discount_value: str = "",
) -> dict:
    """
    Modifie un devis existant (PUT /quotes/{id}).
    Seuls les champs fournis sont envoyes ; les autres restent inchanges.

    Les lignes ne se remplacent pas en bloc : elles se pilotent par operations
    granulaires. Lire d'abord le devis avec pennylane_get_quote pour recuperer
    l'`id` de chaque ligne existante.

    Args:
        quote_id:     OBLIGATOIRE. ID Pennylane du devis.
        customer_id:  Nouveau client (0 = inchange).
        date:         Nouvelle date d'emission (YYYY-MM-DD).
        deadline:     Nouvelle date de fin de validite (YYYY-MM-DD).

        lines_create: Lignes a AJOUTER. Meme format que `lines` de
                      pennylane_create_quote (ligne catalogue avec product_id +
                      quantity, ou ligne libre avec label + quantity +
                      raw_currency_unit_price + unit + vat_rate).

        lines_update: Lignes a MODIFIER. Chaque objet doit contenir l'`id` de la
                      ligne plus les champs a changer, ex:
                      [{"id": 3341207826432, "quantity": 3,
                        "raw_currency_unit_price": "12.50"}]

        lines_delete: Lignes a SUPPRIMER. Liste d'ids ([3341207826432]) ou
                      d'objets ([{"id": 3341207826432}]).

        subject, description, free_text, special_mention, currency, language,
        external_reference, quote_template_id, discount_type, discount_value :
        memes semantiques que pennylane_create_quote.

    SECTIONS : cet outil ne peut PAS les gerer. `PUT /quotes/{id}` n'accepte pas
    `invoice_line_sections` et il n'existe aucun endpoint dedie : impossible
    d'ajouter, renommer ou supprimer une section apres coup. Elles se definissent
    uniquement a la creation, via le parametre `sections` de
    pennylane_create_quote. Le `section_rank` passe ici dans lines_create /
    lines_update doit donc designer une section DEJA existante sur ce devis,
    sinon l'API repond 400.

    Returns:
        Le devis mis a jour normalise, ou {"erreur": ..., "details": ...}.
    """
    if language and language not in QUOTE_LANGUAGES:
        return {"erreur": f"language invalide: {language}",
                "valeurs_autorisees": list(QUOTE_LANGUAGES)}

    body = _quote_body(customer_id, date, deadline, subject, description,
                       free_text, special_mention, currency, language,
                       external_reference, quote_template_id,
                       discount_type, discount_value)

    ops: dict = {}

    creations = _as_lines(lines_create, "lines_create")
    if isinstance(creations, dict):
        return creations
    if creations:
        norm = _norm_lines(creations, _LINE_KEYS_CREATE, "create")
        if isinstance(norm, dict):
            return norm
        ops["create"] = norm

    updates = _as_lines(lines_update, "lines_update")
    if isinstance(updates, dict):
        return updates
    if updates:
        norm = _norm_lines(updates, _LINE_KEYS_UPDATE, "update")
        if isinstance(norm, dict):
            return norm
        ops["update"] = norm

    # lines_delete accepte aussi bien [123] que [{"id": 123}].
    if lines_delete not in (None, "", []):
        raw_del = lines_delete
        if isinstance(raw_del, str):
            try:
                raw_del = json.loads(raw_del)
            except Exception as e:
                return {"erreur": f"lines_delete n'est pas un JSON valide: {e}"}
        if not isinstance(raw_del, list):
            raw_del = [raw_del]
        suppressions = []
        for item in raw_del:
            lid = item.get("id") if isinstance(item, dict) else item
            try:
                suppressions.append({"id": int(lid)})
            except (TypeError, ValueError):
                return {"erreur": f"lines_delete: id de ligne invalide {item!r}."}
        if suppressions:
            ops["delete"] = suppressions

    if ops:
        body["invoice_lines"] = ops
    if not body:
        return {"erreur": "Aucune modification fournie.", "quote_id": quote_id}

    result = await pl_write("PUT", f"/quotes/{quote_id}", body)
    if "erreur" in result:
        return result
    return _quote_out(result)


@mcp.tool(
    name="pennylane_update_quote_status",
    annotations={"readOnlyHint": False, "destructiveHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_update_quote_status(quote_id: str, status: str) -> dict:
    """
    Change le statut d'un devis (PUT /quotes/{id}/update_status).

    Args:
        quote_id: OBLIGATOIRE. ID Pennylane du devis.
        status:   OBLIGATOIRE. Un de :
                  pending  — en attente de reponse du client
                  accepted — devis accepte
                  denied   — devis refuse
                  expired  — devis expire
                  invoiced — devis facture

    Returns:
        Le devis mis a jour normalise, ou {"erreur": ...}.
    """
    if status not in QUOTE_STATUSES:
        return {"erreur": f"status invalide: {status}",
                "valeurs_autorisees": list(QUOTE_STATUSES)}

    result = await pl_write("PUT", f"/quotes/{quote_id}/update_status", {"status": status})
    if "erreur" in result:
        return result
    return _quote_out(result)


# === 7. Abonnements de facturation ===

@mcp.tool(
    name="pennylane_list_billing_subscriptions",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_billing_subscriptions(
    customer_id: str = "",
    status: str = "",
    limit: int = 50,
    cursor: str = "",
) -> dict:
    """
    Liste les abonnements de facturation (factures recurrentes).
    Utile pour croiser avec les abonnements Stripe.

    Args:
        customer_id: Filtrer par client (ID Pennylane).
        status:      Statut de l'abonnement (ex: active, stopped).
        limit:       Resultats par page (1..100).
        cursor:      Curseur de pagination.
    """
    filters = []
    if customer_id:
        filters.append(_field("customer_id", "eq", customer_id))
    if status:
        filters.append(_field("status", "eq", status))

    result = await pl_get("/billing_subscriptions", _page_params(limit, cursor, filters))
    if "erreur" in result:
        return result

    items = [
        {
            "id":                 s.get("id"),
            "client_id":          _ref_id(s, "customer"),
            "label":              s.get("label") or "",
            "statut":             s.get("status", ""),
            "mode":               s.get("mode", ""),
            "debut":              s.get("start", ""),
            "fin":                s.get("finish", ""),
            "prochaine_echeance": s.get("next_occurrence", ""),
            "derniere_echeance":  s.get("prev_occurrence", ""),
            "recurrence":         s.get("recurring_rule") or {},
            "moyen_paiement":     s.get("payment_method", ""),
        }
        for s in _raw_items(result)
    ]
    return _paged(result, items)


@mcp.tool(
    name="pennylane_get_billing_subscription",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_get_billing_subscription(subscription_id: str) -> dict:
    """
    Retourne les details d'un abonnement de facturation avec ses lignes.

    Args:
        subscription_id: ID Pennylane de l'abonnement.
    """
    s = await pl_get(f"/billing_subscriptions/{subscription_id}")
    if "erreur" in s:
        return s

    lignes = await _fetch_lines(f"/billing_subscriptions/{subscription_id}/invoice_lines")
    return {
        "id":                 s.get("id"),
        "client_id":          _ref_id(s, "customer"),
        "label":              s.get("label") or "",
        "statut":             s.get("status", ""),
        "debut":              s.get("start", ""),
        "fin":                s.get("finish", ""),
        "prochaine_echeance": s.get("next_occurrence", ""),
        "recurrence":         s.get("recurring_rule") or {},
        "lignes":             lignes,
    }


# === 8. Comptes bancaires ===

@mcp.tool(
    name="pennylane_list_bank_accounts",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_bank_accounts() -> dict:
    """
    Liste les comptes bancaires connectes a Pennylane (nom, solde, devise).
    """
    result = await pl_get("/bank_accounts", {"limit": MAX_LIMIT})
    if "erreur" in result:
        return result

    items = [
        {
            "id":                    b.get("id"),
            "nom":                   b.get("name", ""),
            "solde":                 b.get("balance", ""),
            "currency":              b.get("currency", "EUR"),
            "bank_establishment_id": _ref_id(b, "bank_establishment"),
        }
        for b in _raw_items(result)
    ]
    return _paged(result, items)


# === 9. Transactions bancaires ===

@mcp.tool(
    name="pennylane_list_transactions",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_list_transactions(
    bank_account_id: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
    cursor: str = "",
) -> dict:
    """
    Liste les transactions bancaires (de la plus recente a la plus ancienne).

    Args:
        bank_account_id: Filtrer par compte bancaire (ID Pennylane).
        date_from:       Date minimale (YYYY-MM-DD).
        date_to:         Date maximale (YYYY-MM-DD).
        limit:           Resultats par page (1..100).
        cursor:          Curseur de pagination.
    """
    filters = []
    if bank_account_id:
        filters.append(_field("bank_account_id", "eq", bank_account_id))
    if date_from:
        filters.append(_field("date", "gteq", date_from))
    if date_to:
        filters.append(_field("date", "lteq", date_to))

    result = await pl_get("/transactions", _page_params(limit, cursor, filters))
    if "erreur" in result:
        return result

    items = [
        {
            "id":              t.get("id"),
            "date":            t.get("date", ""),
            "label":           t.get("label", ""),
            "montant":         t.get("currency_amount", t.get("amount", "")),
            "currency":        t.get("currency", "EUR"),
            "bank_account_id": _ref_id(t, "bank_account"),
            "reste_a_lettrer": t.get("outstanding_balance", ""),
            "justificatif_requis": t.get("attachment_required", False),
        }
        for t in _raw_items(result)
    ]
    return _paged(result, items)


@mcp.tool(
    name="pennylane_get_transaction",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_get_transaction(transaction_id: str) -> dict:
    """
    Retourne les details bruts d'une transaction bancaire.

    Args:
        transaction_id: ID Pennylane de la transaction.
    """
    return await pl_get(f"/transactions/{transaction_id}")


# === 9 bis. Rapprochement facture <-> transaction ===

def _invoice_kind(invoice_type: str) -> Any:
    """Normalise le type de facture ; renvoie {"erreur": ...} si invalide."""
    t = (invoice_type or "").strip().lower()
    if t in ("customer", "client", "vente", "clients"):
        return "customer_invoices"
    if t in ("supplier", "fournisseur", "achat", "fournisseurs"):
        return "supplier_invoices"
    return {"erreur": "invoice_type invalide",
            "detail": "Attendu 'customer' (facture client) ou 'supplier' "
                      f"(facture fournisseur), recu '{invoice_type}'."}


def _to_float(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


@mcp.tool(
    name="pennylane_transaction_matched_invoices",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_transaction_matched_invoices(transaction_id: str) -> dict:
    """
    Liste les factures deja rapprochees d'une transaction bancaire.

    Args:
        transaction_id: ID Pennylane de la transaction.

    Returns:
        items (id, type "customer"/"supplier", url), count. Une liste vide
        signifie que la transaction n'est rapprochee d'aucune facture.
    """
    result = await pl_get(f"/transactions/{transaction_id}/matched_invoices")
    if "erreur" in result:
        return result
    items = [
        {"facture_id": i.get("id"), "type": i.get("type", ""), "url": i.get("url", "")}
        for i in _raw_items(result)
    ]
    return _paged(result, items)


@mcp.tool(
    name="pennylane_match_transaction",
    annotations={"readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False},
)
async def pennylane_match_transaction(
    invoice_id: str,
    transaction_id: str,
    invoice_type: str = "customer",
) -> dict:
    """
    Rapproche UNE transaction bancaire d'UNE facture (lettrage du paiement).

    POST /<customer_invoices|supplier_invoices>/{id}/matched_transactions

    L'API ne traite qu'un couple a la fois : pour rapprocher plusieurs
    transactions d'une meme facture (paiement en plusieurs fois), appeler
    l'outil autant de fois. Une meme transaction peut aussi etre rapprochee
    de plusieurs factures (un virement qui solde plusieurs factures).

    Args:
        invoice_id:     ID Pennylane de la facture.
        transaction_id: ID Pennylane de la transaction bancaire.
        invoice_type:   "customer" (facture client, defaut) ou "supplier"
                        (facture fournisseur). Synonymes FR acceptes.

    Returns:
        {"succes": True, ...} si le rapprochement est enregistre (l'API repond
        204 sans corps), ou {"erreur": ...}.

    ATTENTION : ne fonctionne PAS sur une facture en brouillon (`brouillon`
    a True dans pennylane_list_customer_invoices).
    """
    kind = _invoice_kind(invoice_type)
    if isinstance(kind, dict):
        return kind

    result = await pl_write(
        "POST",
        f"/{kind}/{invoice_id}/matched_transactions",
        {"transaction_id": int(transaction_id)} if str(transaction_id).strip().isdigit()
        else {"transaction_id": transaction_id},
    )
    if "erreur" in result:
        return result
    return {"succes": True, "facture_id": invoice_id, "transaction_id": transaction_id,
            "type": kind, "message": "Transaction rapprochee de la facture."}


@mcp.tool(
    name="pennylane_unmatch_transaction",
    annotations={"readOnlyHint": False, "destructiveHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def pennylane_unmatch_transaction(
    invoice_id: str,
    transaction_id: str,
    invoice_type: str = "customer",
) -> dict:
    """
    Annule le rapprochement entre une transaction et une facture.

    DELETE /<customer_invoices|supplier_invoices>/{id}/matched_transactions/{tx}

    Args:
        invoice_id:     ID Pennylane de la facture.
        transaction_id: ID Pennylane de la transaction a detacher.
        invoice_type:   "customer" (defaut) ou "supplier". Synonymes FR acceptes.

    Returns:
        {"succes": True, ...} si le rapprochement est supprime, ou {"erreur": ...}.
    """
    kind = _invoice_kind(invoice_type)
    if isinstance(kind, dict):
        return kind

    result = await pl_write(
        "DELETE", f"/{kind}/{invoice_id}/matched_transactions/{transaction_id}")
    if "erreur" in result:
        return result
    return {"succes": True, "facture_id": invoice_id, "transaction_id": transaction_id,
            "type": kind, "message": "Rapprochement annule."}


@mcp.tool(
    name="pennylane_suggest_matches",
    annotations={"readOnlyHint": True, "idempotentHint": False, "openWorldHint": False},
)
async def pennylane_suggest_matches(
    invoice_type: str = "customer",
    date_from: str = "",
    date_to: str = "",
    tolerance: str = "0.01",
    jours_ecart: int = 30,
    bank_account_id: str = "",
    max_factures: int = 100,
    max_transactions: int = 200,
) -> dict:
    """
    PROPOSE des rapprochements entre factures impayees et transactions non
    lettrees, en croisant montant et date. NE MODIFIE RIEN : c'est une aide a
    la decision, le rapprochement reel se fait avec pennylane_match_transaction.

    Le croisement est fait cote serveur MCP (l'API v2 n'offre aucun rapprochement
    automatique) : on compare le montant restant du de chaque facture au montant
    de chaque transaction non lettree, en valeur absolue.

    Args:
        invoice_type:     "customer" (defaut) ou "supplier". Synonymes FR acceptes.
        date_from:        Debut de la periode analysee (YYYY-MM-DD), sur les
                          factures ET les transactions.
        date_to:          Fin de la periode analysee (YYYY-MM-DD).
        tolerance:        Ecart de montant tolere, en valeur absolue (defaut
                          "0.01" pour absorber les arrondis). Mettre "0" pour
                          exiger l'egalite stricte.
        jours_ecart:      Ecart maximal en jours entre la date de facture et la
                          date de transaction (defaut 30). 0 = pas de limite.
        bank_account_id:  Restreindre a un compte bancaire.
        max_factures:     Plafond de factures analysees (defaut 100).
        max_transactions: Plafond de transactions analysees (defaut 200).

    Returns:
        propositions : liste de {facture_id, numero, facture_date, reste_du,
        transaction_id, transaction_date, transaction_label, montant,
        ecart_montant, ecart_jours, confiance}.
        `confiance` vaut "exact" (montant au centime et meme jour), "probable"
        (montant dans la tolerance, ecart de dates faible) ou "faible".
        Sont aussi renvoyes factures_sans_correspondance et
        transactions_sans_correspondance pour voir ce qui reste a traiter.

    ATTENTION : une proposition n'est PAS une verite. Plusieurs factures du meme
    montant produisent des propositions ambigues (signalees par `ambigu`).
    Toujours faire valider avant d'appeler pennylane_match_transaction.
    """
    kind = _invoice_kind(invoice_type)
    if isinstance(kind, dict):
        return kind

    tol = _to_float(tolerance)
    if tol is None or tol < 0:
        return {"erreur": f"tolerance invalide: {tolerance!r} (nombre >= 0 attendu)."}

    inv_filters = []
    tx_filters = []
    if date_from:
        inv_filters.append(_field("date", "gteq", date_from))
        tx_filters.append(_field("date", "gteq", date_from))
    if date_to:
        inv_filters.append(_field("date", "lteq", date_to))
        tx_filters.append(_field("date", "lteq", date_to))
    if bank_account_id:
        tx_filters.append(_field("bank_account_id", "eq", int(bank_account_id)))

    inv_raw = await pl_get(f"/{kind}", _page_params(min(max_factures, MAX_LIMIT), "", inv_filters))
    if "erreur" in inv_raw:
        return inv_raw
    tx_raw = await pl_get("/transactions", _page_params(min(max_transactions, MAX_LIMIT), "", tx_filters))
    if "erreur" in tx_raw:
        return tx_raw

    factures = []
    for f in _raw_items(inv_raw):
        if f.get("draft"):
            continue                      # le matching est refuse sur un brouillon
        reste = _to_float(f.get("remaining_amount_with_tax"))
        if reste is None or abs(reste) < 0.005:
            continue                      # deja soldee
        factures.append({
            "id": f.get("id"),
            "numero": f.get("invoice_number", ""),
            "date": f.get("date", ""),
            "reste_du": reste,
        })

    transactions = []
    for t in _raw_items(tx_raw):
        solde = _to_float(t.get("outstanding_balance"))
        if solde is not None and abs(solde) < 0.005:
            continue                      # deja entierement lettree
        montant = _to_float(t.get("currency_amount", t.get("amount")))
        if montant is None:
            continue
        transactions.append({
            "id": t.get("id"),
            "date": t.get("date", ""),
            "label": t.get("label", ""),
            "montant": montant,
        })

    def _jours(d1: str, d2: str) -> int | None:
        try:
            a = datetime.strptime(d1[:10], "%Y-%m-%d")
            b = datetime.strptime(d2[:10], "%Y-%m-%d")
            return abs((a - b).days)
        except Exception:
            return None

    propositions = []
    for f in factures:
        for t in transactions:
            ecart = abs(abs(f["reste_du"]) - abs(t["montant"]))
            if ecart > tol:
                continue
            dj = _jours(f["date"], t["date"])
            if jours_ecart and dj is not None and dj > jours_ecart:
                continue
            if ecart < 0.005 and dj == 0:
                conf = "exact"
            elif dj is not None and dj <= 7:
                conf = "probable"
            else:
                conf = "faible"
            propositions.append({
                "facture_id": f["id"], "numero": f["numero"],
                "facture_date": f["date"], "reste_du": f"{f['reste_du']:.2f}",
                "transaction_id": t["id"], "transaction_date": t["date"],
                "transaction_label": t["label"], "montant": f"{t['montant']:.2f}",
                "ecart_montant": f"{ecart:.2f}",
                "ecart_jours": dj if dj is not None else "",
                "confiance": conf,
            })

    # Une facture (ou une transaction) qui apparait plusieurs fois est ambigue :
    # meme montant sur plusieurs candidats, l'agent ne doit pas trancher seul.
    vus_f = [p["facture_id"] for p in propositions]
    vus_t = [p["transaction_id"] for p in propositions]
    for p in propositions:
        p["ambigu"] = vus_f.count(p["facture_id"]) > 1 or vus_t.count(p["transaction_id"]) > 1

    ordre = {"exact": 0, "probable": 1, "faible": 2}
    propositions.sort(key=lambda p: (ordre[p["confiance"]], float(p["ecart_montant"])))

    appariees_f = {p["facture_id"] for p in propositions}
    appariees_t = {p["transaction_id"] for p in propositions}
    return {
        "propositions": propositions,
        "count": len(propositions),
        "type": kind,
        "factures_analysees": len(factures),
        "transactions_analysees": len(transactions),
        "factures_sans_correspondance": [
            {"facture_id": f["id"], "numero": f["numero"], "date": f["date"],
             "reste_du": f"{f['reste_du']:.2f}"}
            for f in factures if f["id"] not in appariees_f],
        "transactions_sans_correspondance": [
            {"transaction_id": t["id"], "date": t["date"], "label": t["label"],
             "montant": f"{t['montant']:.2f}"}
            for t in transactions if t["id"] not in appariees_t],
        "avertissement": "Propositions non verifiees : valider avant d'appeler "
                         "pennylane_match_transaction, surtout si ambigu=true.",
    }


# === 10. Materiel par client (outil croise) ===

@mcp.tool(
    name="pennylane_customer_equipment",
    annotations={"readOnlyHint": True, "idempotentHint": False, "openWorldHint": False},
)
async def pennylane_customer_equipment(
    customer_id: str,
    keywords: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """
    Agrege toutes les lignes de factures d'un client pour identifier
    le materiel installe (routeur, switch, PC, cablage, etc.).
    Effectue plusieurs appels API en parallele (2 par facture) : peut etre lent
    sur gros volumes.

    Args:
        customer_id: ID Pennylane du client.
        keywords:    Mots-cles separes par virgule pour filtrer les lignes
                     (ex: "routeur,switch,cable"). Vide = tout retourner.
        date_from:   Limiter aux factures depuis cette date (YYYY-MM-DD).
        date_to:     Limiter aux factures jusqu'a cette date (YYYY-MM-DD).

    Returns:
        materiel : lignes avec facture_id, facture_numero, facture_date,
                   label, description, quantite, prix_unitaire, total_ht.
        total_lignes        : nombre de lignes trouvees.
        factures_analysees  : nombre de factures examinees.
    """
    invoices = await pennylane_list_customer_invoices(
        customer_id=customer_id,
        date_from=date_from,
        date_to=date_to,
        limit=MAX_LIMIT,
    )
    if "erreur" in invoices:
        return invoices

    factures = invoices.get("items", [])
    if not factures:
        return {"materiel": [], "total_lignes": 0, "factures_analysees": 0}

    mots = [m.strip().lower() for m in keywords.split(",") if m.strip()] if keywords else []

    async def get_detail(f: dict) -> list:
        lignes = await _fetch_lines(f"/customer_invoices/{f['id']}/invoice_lines")
        out = []
        for ligne in lignes:
            if "erreur" in ligne:
                continue
            texte = f"{ligne.get('label', '')} {ligne.get('description', '')}".lower()
            if mots and not any(m in texte for m in mots):
                continue
            out.append({
                "facture_id":     f.get("id"),
                "facture_numero": f.get("numero", ""),
                "facture_date":   f.get("date", ""),
                "label":          ligne.get("label", ""),
                "description":    ligne.get("description", ""),
                "quantite":       ligne.get("quantite", ""),
                "prix_unitaire":  ligne.get("prix_unitaire", ""),
                "total_ht":       ligne.get("total_ht", ""),
            })
        return out

    results = await asyncio.gather(*[get_detail(f) for f in factures])
    materiel = [item for sublist in results for item in sublist]

    return {
        "materiel":           materiel,
        "total_lignes":       len(materiel),
        "factures_analysees": len(factures),
        "tronque":            bool(invoices.get("has_more")),
    }



# =============================================================================
# Journalisation
# =============================================================================

class JournalOutils(FastMCPMiddleware):
    """Journalise chaque appel d'outil sur stderr (noms d'arguments seulement).

    Les outils d'ecriture comptable sont signales, pour qu'un support puisse
    retrouver dans le journal de Claude ce qui a ete modifie et quand.
    """

    async def on_call_tool(self, context, call_next):
        tool = getattr(context.message, "name", "?")
        args = getattr(context.message, "arguments", None) or {}
        marque = " [ECRITURE]" if tool in ECRITURE else ""
        t0 = time.monotonic()
        try:
            result = await call_next(context)
            log.log(
                logging.WARNING if marque else logging.INFO,
                "tool=%s%s args=%s -> ok (%.0fms)",
                tool, marque, sorted(args), (time.monotonic() - t0) * 1000,
            )
            return result
        except Exception as exc:
            log.warning("tool=%s%s args=%s -> ERREUR %s: %s (%.0fms)",
                        tool, marque, sorted(args), type(exc).__name__, exc,
                        (time.monotonic() - t0) * 1000)
            raise


# Outils qui modifient la comptabilite.
ECRITURE = {
    "pennylane_create_customer",
    "pennylane_match_transaction",
    "pennylane_unmatch_transaction",
    "pennylane_create_product",
    "pennylane_update_product",
    "pennylane_create_quote",
    "pennylane_update_quote",
    "pennylane_update_quote_status",
}


mcp.add_middleware(JournalOutils())


# =============================================================================
# Entrypoint
# =============================================================================

def main() -> None:
    if not API_KEY:
        raise SystemExit(
            "Token API Pennylane introuvable (ni PENNYLANE_API_KEY, ni trousseau "
            f"macOS « {KEYCHAIN_SERVICE} »). Relancer l'installateur. Abandon."
        )
    mcp.run()


if __name__ == "__main__":
    main()
