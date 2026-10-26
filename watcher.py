"""Surveillance de la boîte de support par interrogation IMAP régulière (polling).

Chaque cycle : connexion, recherche des messages UNSEEN dans INBOX, filtrage (boucle,
réponses automatiques, doublons), traitement par l'agent, marquage comme lu.
"""

import json
import logging
import re
import time
from datetime import date, datetime

from bs4 import BeautifulSoup
#MailBox:Permet de se connecter à une boîte e-mail et de récupérer les messages.
#AND:seen=Définit les critères de recherche des e-mails .exp :False signifie que le message n'a pas été marqué comme lu.
#MailMessageFlags:Permet de gérer les indicateurs des e-mails, notamment leur statut lu/non lu.(modifier leurs indicateurs)
from imap_tools import AND, MailBox, MailMessageFlags

from agent.graph import process_email
from config import INCOMING_DIR, settings
from memory import memory_store

logger = logging.getLogger(__name__)
# Détecte les adresses e-mail automatiques (no-reply, mailer-daemon...) pour les ignorer
NO_REPLY_PATTERN = re.compile(r"(no-?reply|do-?not-?reply|mailer-daemon|postmaster)", re.IGNORECASE)


def _header(msg, name):
    values = msg.headers.get(name.lower(), ())
    # Un en-tête long peut être coupé sur plusieurs lignes : on remet tout sur une seule ligne
    return " ".join(values[0].split()) if values else ""


def html_to_text(html):
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def extract_email(msg):
    """Extrait les informations utiles d'un message IMAP."""
    body = (msg.text or "").strip() or html_to_text(msg.html or "")
    message_id = _header(msg, "Message-ID") or f"<uid-{msg.uid}@{settings.imap_host}>"
    return {
        "sender": (msg.from_ or "").strip().lower(),
        "subject": msg.subject or "",
        "body": body,
        "date": msg.date.isoformat() if msg.date else "",
        "message_id": message_id,
        "in_reply_to": _header(msg, "In-Reply-To"),
        "references": _header(msg, "References"),
        "auto_submitted": _header(msg, "Auto-Submitted"),
        "precedence": _header(msg, "Precedence"),
        "uid": msg.uid,
    }


def ignore_reason(email):
    """Retourne la raison d'ignorer le message, ou None s'il doit être traité."""
    sender = email["sender"]
    if settings.support_email and sender == settings.support_email:
        return "envoyé par la boîte de support elle-même"
    if settings.operator_email and sender == settings.operator_email.lower():
        return "envoyé par l'opérateur"
    auto = email.get("auto_submitted", "").lower()
    if auto and auto != "no":
        return f"réponse automatique (Auto-Submitted: {auto})"
    if email.get("precedence", "").lower() in {"bulk", "junk", "list", "auto_reply"}:
        return f"notification (Precedence: {email['precedence']})"
    if NO_REPLY_PATTERN.search(sender):
        return "adresse automatique (no-reply / mailer-daemon)"
    if memory_store.is_processed(email["message_id"]):
        return "Message-ID déjà traité"
    return None


def _save_copy(email):
    folder = INCOMING_DIR / "received"
    folder.mkdir(parents=True, exist_ok=True)
     ## Remplace les caractères spéciaux et les espaces par "_" pour générer un nom de fichier valide
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{datetime.now():%Y%m%d-%H%M%S}_{email['sender']}")
    (folder / f"{name}.json").write_text(json.dumps(email, ensure_ascii=False, indent=2), encoding="utf-8")


def _mailbox():
    return MailBox(settings.imap_host).login(settings.imap_user, settings.imap_password, initial_folder="INBOX")


def _mark_seen(uids):
    # Vérifie qu'il existe des e-mails à marquer comme lus
    if uids:
     # Ouvre une connexion IMAP et la ferme automatiquement à la fin
        with _mailbox() as mailbox:
            mailbox.flag(uids, MailMessageFlags.SEEN, True)


def poll_once(reasoner=None):
    """Un cycle de surveillance. Retourne le nombre d'e-mails traités par l'agent."""
    with _mailbox() as mailbox:
        # mark_seen=False : on ne marque comme lu qu'après traitement
        # date_gte=date.today() : seulement les e-mails reçus aujourd'hui (on ignore les vieux e-mails de la boîte)
        messages = [extract_email(m) for m in mailbox.fetch(AND(seen=False, date_gte=date.today()), mark_seen=False)]

    processed = 0
    for email in messages:
        reason = ignore_reason(email)
        if reason:
            logger.info("Ignoré : %s (%s)", email["sender"], reason)
            _mark_seen([email["uid"]])
            continue

        logger.info("Nouveau mail de %s : %s", email["sender"], email["subject"])
        _save_copy(email)
        try:
            report = process_email(email, reasoner=reasoner)
            logger.info("Résultat : %s (%s)", report["outcome"], report.get("report_file"))
            processed += 1
        except Exception:  # process_email ne lève pas, sécurité supplémentaire
            logger.exception("Erreur inattendue sur le mail de %s", email["sender"])
        finally:
            # Marqué comme lu même en cas d'erreur (l'opérateur a été prévenu) pour éviter une boucle
            memory_store.mark_processed(email["message_id"])
            _mark_seen([email["uid"]])
    return processed


def run_forever(interval=None):
        # Définit le temps d'attente entre deux vérifications des e-mails
    interval = interval or settings.poll_interval_seconds
    # Vérifie que les identifiants IMAP sont renseignés
    if not (settings.imap_user and settings.imap_password):
        raise SystemExit("IMAP_USER et IMAP_PASSWORD doivent être définis dans le fichier .env")
     # Affiche les informations de surveillance
    logger.info("Surveillance de %s toutes les %d s (DRY_RUN=%s). Ctrl+C pour arrêter.",
                settings.imap_user, interval, settings.dry_run)
    reasoner = None
    while True:
        try:
            if reasoner is None:
                from agent.graph import GeminiReasoner
                reasoner = GeminiReasoner()
            poll_once(reasoner)
        except KeyboardInterrupt:
            raise
        except Exception:
            # Erreur de connexion IMAP, réseau... : on réessaie au cycle suivant
            logger.exception("Erreur pendant la vérification de la boîte")
        time.sleep(interval)
