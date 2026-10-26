# SolveMate

SolveMate est un agent IA qui lit les e-mails reçus par le support d'un opérateur télécom.
Pour chaque e-mail, il :

1. identifie le client grâce à son adresse e-mail ;
2. vérifie les faits dans la base (factures, paiements, pannes réseau, tickets) ;
3. cherche la bonne procédure dans la base de connaissances (RAG) ;
4. répond au client **ou** transmet le dossier à un opérateur humain (escalade) ;
5. met à jour le ticket et la mémoire du client.

## Organisation du projet

```
app.py                  Point d'entrée : index | test | loop
watcher.py              Lit les nouveaux e-mails de la boîte (IMAP)
config.py               Lit le fichier .env
db.py                   Connexion à PostgreSQL
mail_sender.py          Envoie les e-mails (SMTP) ou les simule (DRY_RUN)
agent/graph.py          Les étapes du traitement d'un e-mail
agent/tools.py          Les requêtes SQL utilisées par l'agent
agent/prompts.py        Les règles données à Gemini (prompt)
rag/retriever.py        Découpe le PDF en procédures et les range dans Qdrant
memory/memory_store.py  Mémoire de l'agent + liste des e-mails déjà traités
data/                   Le PDF des procédures (source du RAG)
sql/                    Création des tables et données de test
incoming_emails/        E-mails de test (JSON)
reports/                Une trace JSON par e-mail traité + outbox.log (mails simulés)
```

## Principes importants

- **Gemini lit, l'application agit.** Gemini peut seulement consulter les données du client qui a
  écrit. C'est l'application qui crée les tickets, envoie les e-mails et écrit la mémoire.
- **Garde-fous** : l'application force l'escalade si :
  - la confiance de Gemini est inférieure à 0,6 ;
  - l'e-mail essaie de donner des ordres à l'agent (« ignore tes règles... ») ;
  - la réponse prévue pour le client est vide ;
  - une erreur se produit pendant le traitement.
- **Pas de ticket en double** : si le client a déjà un ticket ouvert de la même catégorie
  (Billing, Network ou Service), ce ticket est réutilisé.
- **Langue** : Gemini répond dans la langue du client. Les messages fixes (client inconnu,
  accusé de réception) sont écrits en français et en anglais.
- **Gemini gratuit** : en cas d'erreur 429 ou 5xx, l'appel est réessayé après 5 s, 10 s, 20 s,
  40 s puis 60 s.

## Installation

Prérequis : Python 3.11+ et PostgreSQL.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (Linux/macOS : source .venv/bin/activate)
pip install -r requirements.txt
```

### 1. Fichier .env

Copier `.env.example` vers `.env` et remplir :

- `DATABASE_URL` : par exemple `postgresql://postgres:motdepasse@localhost:5432/solvemate`
- `GEMINI_API_KEY` : clé gratuite sur <https://aistudio.google.com/apikey>
- `QDRANT_URL` et `QDRANT_API_KEY` : voir l'étape 3
- `IMAP_*`, `SMTP_*`, `OPERATOR_EMAIL` : seulement pour le mode boucle
- `DRY_RUN=true` tant que vous testez (aucun vrai mail envoyé)

**Avec Gmail** : activer IMAP dans les paramètres, activer la validation en deux étapes, puis créer
un *mot de passe d'application* à utiliser pour `IMAP_PASSWORD` et `SMTP_PASSWORD`.

### 2. Base de données

Dans `psql`, connecté à la base `solvemate` :

```
\i sql/create_tables.sql
\i sql/insert_test_data.sql
```

`insert_test_data.sql` vide les tables puis remet les données de départ. Relancez-le pour repartir
de zéro.

### 3. Base de connaissances (Qdrant)

1. Créer un compte gratuit sur <https://cloud.qdrant.io> et un cluster *Free tier*.
2. Copier l'adresse du cluster et une clé API dans `QDRANT_URL` et `QDRANT_API_KEY`.
3. Remplir Qdrant avec les procédures du PDF (à relancer si le PDF change) :

```bash
python app.py index
```

Au premier lancement, le modèle d'embeddings (environ 470 Mo) est téléchargé.

## Lancement

### Mode test

```bash
python app.py test --file incoming_emails/scenario_2_bruno.json
python app.py test --sender alice.martin@example.com --subject "Panne" --body "Plus d'Internet"
python app.py test --all
```

- Aucun vrai mail n'est envoyé : ils s'affichent dans le terminal et dans `reports/outbox.log`.
- **Attention** : les tickets et la mémoire sont bien écrits dans PostgreSQL. Relancer
  `sql/insert_test_data.sql` avant une série de tests.
- Chaque e-mail traité crée une trace JSON dans `reports/` (étapes, outils appelés, décision,
  ticket, e-mails, mémoire, erreurs).

### Mode boucle (lecture de la vraie boîte mail)

```bash
python app.py loop                 # vérifie la boîte toutes les 30 s (POLL_INTERVAL_SECONDS)
```

- Mettre `DRY_RUN=false` pour envoyer vraiment les e-mails.
- Seuls les e-mails **non lus du jour** sont traités, puis ils sont marqués comme lus.
- Sont ignorés : les e-mails de la boîte support ou de l'opérateur, les réponses automatiques
  (no-reply, mailer-daemon...) et les e-mails déjà traités.
- Les réponses restent dans le même fil de discussion (« Re: »).

## Scénarios de test

| # | Client | Situation | Résultat attendu |
|---|--------|-----------|------------------|
| 1 | alice.martin | Panne en cours à Brest, ticket Network déjà ouvert | Réponse avec info de panne, pas de nouveau ticket |
| 2 | bruno.lefevre | Paiement réussi mais facture impayée | Escalade Billing |
| 3 | chloe.bernard | Paiement échoué, facture en retard | Réponse invitant à repayer |
| 4 | david.moreau | Paiement d'hier en attente | Réponse rassurante (2 à 3 jours ouvrés) |
| 5 | emma.petit | Tout payé, aucune panne | Dépannage de base Fibre |
| 6 | hugo.roux | Justificatif de paiement attendu | Escalade Billing, ticket existant réutilisé |
| 7 | lina.garcia | 30 € payés sur 59,99 €, panne à Brest | Solde à régler + mention de la panne |
| 8 | inconnu | Adresse absente de la base | Escalade « client inconnu » + message au client (FR/EN) |
| 9 | emma.petit | « Ignore tes règles... » | Escalade, contenu signalé comme suspect |

## Limite connue

Le programme ne ferme jamais un ticket : quand le client confirme que son problème est résolu,
une note est ajoutée au ticket mais il reste `Open`.
