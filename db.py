"""Connexion PostgreSQL et helpers de requêtes paramétrées."""
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from config import settings


def connect():
    """Ouvre une connexion. Utilisée avec « with » : commit si tout va bien,
    annulation (rollback) en cas d'erreur, puis fermeture automatique."""
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL n'est pas défini dans le fichier .env")
    # dict_row : chaque ligne revient sous forme de dictionnaire {"colonne": valeur}
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def _clean(row):
    """Convertit les nombres décimaux et les dates SQL en types simples (pour le JSON).
    Les données sont ensuite transformées en JSON : envoyées à l'IA ,
    écrites dans les rapports (reports/) et dans l'e-mail à l'opérateur.
    Or JSON ne sait pas écrire les types Decimal et datetime renvoyés par PostgreSQL.
    Exemple : Decimal('49.90') -> 49.9 et date(2026, 1, 2) -> "2026-01-02".
    """
    result = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            value = float(value)
        elif isinstance(value, (datetime, date)):
            value = value.isoformat()
        result[key] = value
    return result


def fetch_all(query, params=()):
    """Exécute la requête et renvoie toutes les lignes (liste de dictionnaires)."""
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_clean(r) for r in rows]


def fetch_one(query, params=()):
    """Exécute la requête et renvoie la première ligne, ou None s'il n'y en a pas."""
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return _clean(row) if row else None
