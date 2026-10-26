"""RAG avec Qdrant : recherche des procédures les plus proches d'une question.

1. On lit le PDF et on le découpe en procédures (PAY-01, NET-03...).
2. On transforme chaque procédure en vecteur (une liste de 384 nombres qui représente son sens).
3. On range les vecteurs et les textes dans Qdrant Cloud, une base de données vectorielle.
   Interface web pour vérifier les données : https://cloud.qdrant.io → cluster → Dashboard
4. Pour une question, on la transforme en vecteur et Qdrant trouve les procédures les plus proches.
"""
import re

from config import KNOWLEDGE_FILE, settings

EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
COLLECTION_NAME = "procedures"   # le nom de la « table » dans Qdrant

# Titre d'une procédure : un code comme "PAY-01" ou "NET-03", un tiret, puis le nom
PROCEDURE_TITLE = re.compile(r"^([A-Z]{3}-\d{2}\s*[—–-]\s*.+)$", re.MULTILINE)
# Début d'une rubrique dans une procédure
FIELD_START = re.compile(r"^(Condition|Action|Réponse|Suite|Limite)\s*:")

# Remplis une seule fois, à la première utilisation
_model = None      # le modèle qui transforme un texte en vecteur
_client = None     # la connexion à Qdrant


# ---------------------- Lecture du PDF ----------------------

def read_pdf_text(path):
    """Extrait le texte de toutes les pages du PDF."""
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def split_sections(text):
    """Découpe le texte du PDF en sections, une par procédure (PAY-01, NET-03...)."""
    parts = PROCEDURE_TITLE.split(text)
    # parts = [introduction, titre 1, contenu 1, titre 2, contenu 2, ...]
    sections = []
    for title, body in zip(parts[1::2], parts[2::2]):
        # Le PDF coupe les phrases longues sur plusieurs lignes : on les recolle,
        # en gardant une ligne par rubrique (Condition, Action...)
        lines = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            if lines and not FIELD_START.match(line):
                lines[-1] += " " + line
            else:
                lines.append(line)
        sections.append({"title": title.strip(), "content": "\n".join(lines)})
    return sections


# ---------------------- Vecteurs et base Qdrant ----------------------

def _embed(texts):
    """Transforme chaque texte en vecteur de 384 nombres."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model.encode(texts, normalize_embeddings=True).tolist()


def _qdrant():
    """Se connecte à Qdrant Cloud avec l'adresse et la clé API du fichier .env."""
    global _client
    if _client is None:
        if not settings.qdrant_url or not settings.qdrant_api_key:
            raise RuntimeError("QDRANT_URL et QDRANT_API_KEY doivent être définis dans le fichier .env")
        from qdrant_client import QdrantClient
        _client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    return _client


def build_index():
    """Lit le PDF et (re)remplit la collection Qdrant. Retourne le nombre de procédures.
    À relancer (python app.py index) quand le PDF change."""
    from qdrant_client import models

    client = _qdrant()
    sections = split_sections(read_pdf_text(KNOWLEDGE_FILE))
    vectors = _embed([f"{s['title']}\n{s['content']}" for s in sections])

    # On repart d'une collection vide : vecteurs de 384 nombres, comparés par cosinus
    if client.collection_exists(COLLECTION_NAME):
        client.delete_collection(COLLECTION_NAME)
    client.create_collection(
        COLLECTION_NAME,
        vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE),
    )

    # Un « point » Qdrant = un numéro + un vecteur + des données lisibles (payload)
    client.upsert(COLLECTION_NAME, points=[
        models.PointStruct(
            id=i,
            vector=vector,
            payload={"code": s["title"][:6], "title": s["title"], "content": s["content"]},
        )
        for i, (s, vector) in enumerate(zip(sections, vectors))
    ])
    return len(sections)


def search(query, k=3):
    """Retourne les k procédures les plus proches de la question."""
    client = _qdrant()
    if not client.collection_exists(COLLECTION_NAME):
        build_index()  # première utilisation : on remplit la base

    points = client.query_points(COLLECTION_NAME, query=_embed([query])[0], limit=k).points
    # score : 1 = même sens, 0 = aucun rapport
    return [
        {"title": p.payload["title"], "content": p.payload["content"], "score": round(p.score, 3)}
        for p in points
    ]
