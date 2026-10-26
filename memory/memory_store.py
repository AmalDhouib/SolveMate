"""Mémoire de l'agent (table crm.agent_memory) et suivi des e-mails déjà traités."""
import json

from config import MEMORY_DIR
from db import fetch_all, fetch_one

# Les 3 états possibles d'un dossier
MEMORY_STATES = ("WaitingCustomer", "Escalated", "Resolved")

# Fichier qui contient les Message-ID des e-mails déjà traités
PROCESSED_IDS_FILE = MEMORY_DIR / "processed_ids.json"


# ---------------------- Mémoire PostgreSQL ----------------------

def get_memory(customer_id, limit=3):
    """Renvoie les derniers souvenirs du client, du plus récent au plus ancien."""
    return fetch_all(
        """
        SELECT memory_id, ticket_id, email_subject, summary, action_taken, state, created_at
        FROM crm.agent_memory
        WHERE customer_id = %s
        ORDER BY created_at DESC, memory_id DESC
        LIMIT %s
        """,
        (customer_id, limit),
    )


def save_memory(customer_id, ticket_id, email_subject, summary, action_taken, state):
    """Enregistre un souvenir à la fin du traitement d'un e-mail."""
    if state not in MEMORY_STATES:
        raise ValueError(f"État de mémoire invalide : {state}")

    # On coupe le sujet et l'action à la taille des colonnes (VARCHAR(200) et VARCHAR(100))
    email_subject = (email_subject or "")[:200]
    action_taken = (action_taken or "")[:100]

    return fetch_one(
        """
        INSERT INTO crm.agent_memory
            (customer_id, ticket_id, email_subject, summary, action_taken, state)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING memory_id, created_at
        """,
        (customer_id, ticket_id, email_subject, summary, action_taken, state),
    )


# ---------------------- Message-ID déjà traités ----------------------

def _load_ids():
    """Lit la liste des Message-ID déjà traités (vide si le fichier n'existe pas ou est abîmé)."""
    if not PROCESSED_IDS_FILE.exists():
        return set()
    try:
        return set(json.loads(PROCESSED_IDS_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def is_processed(message_id):
    """True si cet e-mail a déjà été traité."""
    if not message_id:
        return False
    return message_id in _load_ids()


def mark_processed(message_id):
    """Ajoute le Message-ID au fichier pour ne jamais retraiter cet e-mail."""
    if not message_id:
        return
    ids = _load_ids()
    ids.add(message_id)
    PROCESSED_IDS_FILE.write_text(json.dumps(sorted(ids), indent=1), encoding="utf-8")
