"""Lecture de la configuration depuis le fichier .env"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Dossier du projet, puis lecture du fichier .env
BASE_DIR = Path(__file__).resolve().parent
#Lire le fichier .env
load_dotenv(BASE_DIR / ".env")

# Dossiers et fichiers du projet
KNOWLEDGE_FILE = BASE_DIR / "data" / "SolveMate_RAG_Procedures_Corrigees.pdf"
REPORTS_DIR = BASE_DIR / "reports"
MEMORY_DIR = BASE_DIR / "memory"
INCOMING_DIR = BASE_DIR / "incoming_emails"
SQL_DIR = BASE_DIR / "sql"


class Settings:
    """Récupèrer les valeurs lus dans le .env (valeur par défaut si absente)."""

    # Base de données et IA
    database_url = os.getenv("DATABASE_URL", "")
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Toutes les combien de secondes on regarde la boîte mail
    poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))

    # Lecture des e-mails (IMAP)
    imap_host = os.getenv("IMAP_HOST", "imap.gmail.com")
    imap_user = os.getenv("IMAP_USER", "")
    imap_password = os.getenv("IMAP_PASSWORD", "")

    # Envoi des e-mails (SMTP)
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")

    # Personne qui reçoit les escalades
    operator_email = os.getenv("OPERATOR_EMAIL", "")

    # true = simulation (aucun e-mail envoyé). Par défaut : true, par sécurité
    dry_run = os.getenv("DRY_RUN", "true").strip().lower() == "true"

    # Base vectorielle Qdrant Cloud (gratuite) : adresse du cluster et clé API
    qdrant_url = os.getenv("QDRANT_URL", "")
    qdrant_api_key = os.getenv("QDRANT_API_KEY", "")

    # En dessous de cette confiance, l'IA passe la main à l'opérateur
    confidence_threshold = 0.6

    # Adresse du support, pour ne pas se répondre à soi-même
    support_email = (imap_user or smtp_user).lower()


settings = Settings()
