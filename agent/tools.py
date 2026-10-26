"""Outils de l'agent : requêtes SQL paramétrées écrites à l'avance, recherche RAG, envoi d'e-mails.
Le LLM ne génère jamais de SQL. Il n'accède aux outils de lecture qu'à travers `ToolBox`,
qui est liée au client identifié par l'expéditeur : le modèle ne fournit jamais de
customer_id, il ne peut donc pas lire les données d'un autre client.
Les outils d'écriture et d'envoi sont exécutés par l'application (agent/graph.py), pas par le LLM.
"""
import json

from google.genai import types

from config import settings
from db import fetch_all, fetch_one
from mail_sender import send_email
from memory import memory_store
from rag import retriever

TICKET_CATEGORIES = ("Billing", "Network", "Service")
TICKET_PRIORITIES = ("Low", "Medium", "High")
TICKET_STATUSES = ("Open", "In Progress", "Closed")


# =====================================================================
# Outils de lecture
# =====================================================================

def get_customer(email):
    """Profil du client à partir de son adresse e-mail."""
    customer = fetch_one(
        """
        SELECT customer_id, name, email, subscription_type, network_zone
        FROM crm.customers
        WHERE LOWER(email) = LOWER(%s)
        """,
        (email.strip(),),
    )
    return customer or {"status": "introuvable"}


def get_open_tickets(customer_id):
    """Tickets du client encore ouvert"""
    return fetch_all(
        """
        SELECT ticket_id, category, status, priority, description, created_at
        FROM crm.tickets
        WHERE customer_id = %s AND status IN ('Open', 'In Progress')
        ORDER BY created_at DESC
        """,
        (customer_id,),
    )


def get_memory(customer_id):
    """Les 3 derniers souvenirs de l'agent pour ce client."""
    return memory_store.get_memory(customer_id, limit=3)


def get_invoices(customer_id):
    """Les 5 dernières factures du client."""
    return fetch_all(
        """
        SELECT invoice_id, amount, due_date, status,
               (due_date < CURRENT_DATE) AS due_date_passed
        FROM billing.invoices
        WHERE customer_id = %s
        ORDER BY due_date DESC, invoice_id DESC
        LIMIT 5
        """,
        (customer_id,),
    )


def get_payments(customer_id):
    """Les paiements du client, avec le montant et le statut de la facture liée."""
    return fetch_all(
        """
        SELECT p.payment_id, p.invoice_id, p.amount, p.payment_date, p.status,
               i.amount AS invoice_amount, i.status AS invoice_status
        FROM billing.payments p
        JOIN billing.invoices i ON i.invoice_id = p.invoice_id
        WHERE i.customer_id = %s
        ORDER BY p.payment_date DESC
        """,
        (customer_id,),
    )


def get_network_incidents(customer_id):
    """Incidents de la zone du client : Ongoing, ou résolus depuis moins de 48 heures."""
    return fetch_all(
        """
        SELECT n.incident_id, n.affected_area, n.incident_type, n.status,
               n.started_at, n.resolved_at, n.description,
               ROUND(EXTRACT(EPOCH FROM (COALESCE(n.resolved_at, NOW()) - n.started_at)) / 3600, 1)
                   AS duration_hours
        FROM oss.network_incidents n
        JOIN crm.customers c ON c.network_zone = n.affected_area
        WHERE c.customer_id = %s
          AND (n.status = 'Ongoing'
               OR (n.status = 'Resolved' AND n.resolved_at >= NOW() - INTERVAL '48 hours'))
        ORDER BY n.started_at DESC
        """,
        (customer_id,),
    )


def search_knowledge(query):
    """Les 3 procédures de la base de connaissances les plus proches de la question."""
    return retriever.search(query, k=3)


# =====================================================================
# Outils d'écriture (limités et contrôlés)
# =====================================================================

def create_ticket(customer_id, category, priority, description):
    """Crée un ticket, sauf si un ticket ouvert de même catégorie existe déjà (il est alors retourné)."""
    if category not in TICKET_CATEGORIES:
        raise ValueError(f"Catégorie invalide : {category}")
    if priority not in TICKET_PRIORITIES:
        priority = "Medium"

    existing = fetch_one(
        """
        SELECT ticket_id, category, status, priority, description, created_at
        FROM crm.tickets
        WHERE customer_id = %s AND category = %s AND status IN ('Open', 'In Progress')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (customer_id, category),
    )
    if existing:
        return {"created": False, "reason": "ticket ouvert existant", "ticket": existing}

    ticket = fetch_one(
        """
        INSERT INTO crm.tickets (customer_id, category, status, priority, description)
        VALUES (%s, %s, 'Open', %s, %s)
        RETURNING ticket_id, category, status, priority, description, created_at
        """,
        (customer_id, category, priority, description),
    )
    return {"created": True, "ticket": ticket}


def update_ticket(ticket_id, status, description, customer_id=None):
    """Met à jour le statut d'un ticket et ajoute une note à sa description.

    Si customer_id est fourni, la mise à jour n'a lieu que si le ticket appartient à ce client.
    """
    if status not in TICKET_STATUSES:
        raise ValueError(f"Statut invalide : {status}")
    ticket = fetch_one(
        """
        UPDATE crm.tickets
        SET status = %s,
            description = COALESCE(description, '') || E'\n---\n' || %s
        WHERE ticket_id = %s AND (%s::int IS NULL OR customer_id = %s::int)
        RETURNING ticket_id, category, status, priority, description, created_at
        """,
        (status, description, ticket_id, customer_id, customer_id),
    )
    return {"updated": bool(ticket), "ticket": ticket}


def save_memory(customer_id, ticket_id, email_subject, summary, action_taken, state):
    """Enregistre un souvenir à la fin du traitement d'un e-mail."""
    return memory_store.save_memory(customer_id, ticket_id, email_subject, summary, action_taken, state)


# =====================================================================
# Outils d'envoi
# =====================================================================

def send_email_to_customer(customer_email, subject, body, in_reply_to=None, references=None):
    """Répond au client (in_reply_to / references : pour rester dans le même fil de discussion)."""
    return send_email(customer_email, subject, body, in_reply_to=in_reply_to, references=references)


def escalate_to_operator(customer_id, subject, body):
    """Envoie le dossier à l'opérateur humain (customer_id vaut None si le client est inconnu)."""
    if not settings.operator_email:
        raise RuntimeError("OPERATOR_EMAIL n'est pas défini dans le fichier .env")
    prefix = f"[SolveMate][Client {customer_id}]" if customer_id else "[SolveMate][Client inconnu]"
    return send_email(settings.operator_email, f"{prefix} {subject}", body)


# =====================================================================
# Boîte à outils exposée au LLM (liée au client identifié)
# =====================================================================

_NO_ARGS = None
#parameters
_QUERY_ARGS = types.Schema(
    type=types.Type.OBJECT,
    properties={
        # ce que Gemini doit écrire quand il veut chercher dans les procédures.
        "query": types.Schema(
            type=types.Type.STRING,
            description="Description de la situation à rechercher, en français "
            "(ex. « paiement Completed mais facture Unpaid »).",
        )
    },
    #ce parametres est obligatoire
    required=["query"],
)

# (nom de l'outil, description lue par Gemini, paramètres)
LLM_TOOL_SPECS = [
    ("get_customer", "Profil du client qui a écrit : nom, type d'abonnement, zone réseau.", _NO_ARGS),
    ("get_open_tickets", "Tickets du client au statut Open ou In Progress.", _NO_ARGS),
    ("get_memory", "Les 3 derniers souvenirs de l'agent pour ce client (résumé, action, état, date).", _NO_ARGS),
    ("get_invoices", "Les 5 dernières factures du client : montant, échéance, statut.", _NO_ARGS),
    ("get_payments", "Les paiements du client avec la facture liée : montant, date, statut, montant et statut de la facture.", _NO_ARGS),
    ("get_network_incidents", "Incidents réseau dans la zone du client : en cours, ou résolus depuis moins de 48 h, avec leur durée en heures.", _NO_ARGS),
    ("search_knowledge", "Recherche les 3 procédures les plus pertinentes dans la base de connaissances.", _QUERY_ARGS),
]


class ToolBox:
    """Outils de lecture liés à un client. Chaque appel est enregistré pour la trace.
    Une ToolBox est créée pour chaque e-mail, avec le client identifié par l'adresse de l'expéditeur."""

    def __init__(self, customer):
        self.customer = customer
        # Le client est fixé ici par le code, une fois pour toutes : Gemini ne peut pas le changer
        self.customer_id = customer["customer_id"]
        self.calls = []          # tous les appels d'outils (pour le rapport)
        self.rag_sections = []   # carnet : ce que Gemini a cherché dans le PDF et les procédures trouvées
        # Nom de l'outil -> fonction à exécuter.
        # « lambda: ... » = petite fonction sans nom, exécutée seulement quand Gemini demande l'outil.
        # Elles utilisent toujours self.customer_id : impossible de lire les données d'un autre client.
        self._handlers = {
            "get_customer": lambda: self.customer,
            "get_open_tickets": lambda: get_open_tickets(self.customer_id),
            "get_memory": lambda: get_memory(self.customer_id),
            "get_invoices": lambda: get_invoices(self.customer_id),
            "get_payments": lambda: get_payments(self.customer_id),
            "get_network_incidents": lambda: get_network_incidents(self.customer_id),
            "search_knowledge": self._search_knowledge,
        }

    def _search_knowledge(self, query=""):
        """Recherche dans le PDF, et note dans le carnet ce qui a été cherché et trouvé."""
        hits = search_knowledge(query)  # les 3 procédures les plus proches
        # On ne garde que les titres (ex. « PAY-01 — ... ») : le carnet va dans le rapport JSON
        self.rag_sections.append({"query": query, "sections": [h["title"] for h in hits]})
        return hits  # le texte complet des procédures est renvoyé à Gemini

    @staticmethod
    def declarations():
        """Le « menu » des outils envoyé à Gemini : ce qu'il a le droit de demander
        (nom, description, paramètres). C'est Gemini qui choisit lesquels appeler.
        Le format de sa réponse finale est défini ailleurs, dans agent/prompts.py."""
        # Pour chaque ligne de LLM_TOOL_SPECS, on crée une « déclaration » au format Gemini,
        # puis on les range toutes dans un seul paquet (types.Tool)
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(name=name, description=desc, parameters=params)
                    for name, desc, params in LLM_TOOL_SPECS
                ]
            )
        ]

    def call(self, name, args):
        """Exécute l'outil demandé par Gemini et RETOURNE son résultat (n'envoie rien à Gemini).
        Le résultat est rendu à agent/graph.py, qui l'emballe (to_response) puis l'envoie à Gemini.
        L'appel est aussi noté dans self.calls pour le rapport JSON (reports/).
        Exemple : name = "get_invoices", args = {}  ou  name = "search_knowledge", args = {"query": "..."}"""
        args = dict(args or {})              # copie des paramètres envoyés par Gemini ({} s'il n'y en a pas)
        handler = self._handlers.get(name)   # la fonction qui correspond au nom de l'outil

        if handler is None:
            # Gemini a demandé un outil qui n'existe pas : on lui répond par une erreur
            result = {"error": f"Outil inconnu : {name}"}
        else:
            # Sécurité : si Gemini invente un customer_id ou un email (erreur ou manipulation),
            # on les supprime. Le client reste celui fixé dans __init__.
            args.pop("customer_id", None)
            args.pop("email", None)
            try:
                # **args « déballe » le dictionnaire : {"query": "facture"} -> query="facture"
                result = handler(**args)
            except Exception as exc:
                # En cas d'erreur, on prévient Gemini au lieu de faire planter le programme
                result = {"error": f"{type(exc).__name__}: {exc}"}

        # On note l'appel (outil, paramètres, résultat) pour le rapport JSON dans reports/
        self.calls.append({"tool": name, "args": args, "result": result})
        return result

    @staticmethod
    def to_response(result):
        """Emballe le résultat d'un outil avant de l'envoyer à Gemini.
         : Gemini exige un dictionnaire, or certains outils renvoient une liste.
        Exemple : [{"invoice_id": 3, ...}]  ->  {"result": [{"invoice_id": 3, ...}]}
        """
        return {"result": json.loads(json.dumps(result, default=str))}
