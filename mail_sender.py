"""Envoi des e-mails par SMTP, avec le mode DRY_RUN (affichage + log, aucun envoi)."""

import logging
#- SMTP (smtplib) : envoie les réponses aux clients et les notifications aux opérateurs.
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from config import REPORTS_DIR, settings

logger = logging.getLogger(__name__)
OUTBOX_LOG = REPORTS_DIR / "outbox.log"


def reply_subject(subject):
    """Préfixe « Re: » une seule fois."""
    subject = (subject or "").strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()


def send_email(to, subject, body, in_reply_to=None, references=None, dry_run=None):
    """Envoie un e-mail (ou le simule). Retourne un enregistrement pour les logs."""
    dry_run = settings.dry_run if dry_run is None else dry_run 
    #si dry_run(mode simulation:il permet de tester certaines actions sans les exécuter réellement.) n'a pas été précisé, utiliser la valeur définie dans la configuration. Sinon, conserver la valeur fournie.
 
    msg = EmailMessage()
    msg["From"] = settings.smtp_user or settings.support_email or "support@solvemate.local"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="solvemate.local")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to #in_reply_to : identifiant du message auquel SolveMate répond
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to#references : identifiants des messages précédents de la conversation
    msg.set_content(body)

    record = {
        "to": to,
        "subject": subject,
        "body": body,
        "in_reply_to": in_reply_to,
        "references": msg["References"],
        "dry_run": dry_run,
        "sent_at": datetime.now().isoformat(timespec="seconds"),
    }

    if dry_run:
        #une ligne de 70 signes = pour séparer les e-mails dans les logs
        separator = "=" * 70
        text = (
            f"{separator}\n[DRY_RUN] {record['sent_at']}\nÀ : {to}\nSujet : {subject}\n"
            f"In-Reply-To : {in_reply_to or '-'}\n{'-' * 70}\n{body}\n{separator}\n"
        )
        print(text)
        REPORTS_DIR.mkdir(exist_ok=True)
        with OUTBOX_LOG.open("a", encoding="utf-8") as f:
            f.write(text)
        return record
    # Se connecte au serveur SMTP
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()# Sécurise la connexion avec TLS
        smtp.login(settings.smtp_user, settings.smtp_password)# Envoie réellement l'e-mail
        smtp.send_message(msg)
    # Enregistre l'envoi dans les logs
    logger.info("E-mail envoyé à %s : %s", to, subject)
    return record
