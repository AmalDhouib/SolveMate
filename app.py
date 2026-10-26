"""Point d'entrée SolveMate.

Exemples :
    python app.py index                        # remplit Qdrant avec les procédures du PDF
    python app.py test --file incoming_emails/scenario_1_alice.json
    python app.py test --sender alice.martin@example.com --subject "Panne" --body "Plus d'Internet"
    python app.py test --all                   # rejoue tous les e-mails de incoming_emails/
    python app.py loop                         # surveillance de la boîte toutes les 30 s
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from config import INCOMING_DIR, REPORTS_DIR, settings


def _setup_logging(verbose):
    # Niveau DEBUG avec -v, sinon INFO
    #verbose = True → DEBUG : affiche beaucoup de détails, utiles pour comprendre les problèmes.
    #verbose = False → INFO : affiche seulement les informations importantes, ainsi que les avertissements et erreurs.
    niveau = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=niveau, format="%(asctime)s %(levelname)s : %(message)s", datefmt="%H:%M:%S")

    # On cache les messages trop bavards des librairies externes
    #Ces bibliothèques peuvent afficher beaucoup de messages techniques.Cette boucle leur demande de n'afficher que les avertissements (WARNING) et les erreurs, plutôt que tous leurs détails
    for nom in ["httpx", "httpcore", "sentence_transformers", "google_genai"]:
        logging.getLogger(nom).setLevel(logging.WARNING)


def cmd_index(args):
    from rag.retriever import build_index

    print(f"{build_index()} procédures enregistrées dans Qdrant.")
    print(f"Vérifier : {settings.qdrant_url}/dashboard (ou cloud.qdrant.io → cluster → Dashboard)")


def _print_summary(report):
    decision = report.get("decision") or {}
    print("\n" + "#" * 70)
    print(f"Expéditeur : {report['received_email'].get('sender')}")
    print(f"Résultat   : {report['outcome']}")
    if decision:
        print(f"Catégorie  : {decision['category']} | Priorité : {decision['priority']} "
              f"| Confiance : {decision['confidence']}")
        print(f"Diagnostic : {decision['diagnosis']}")
    if report["guardrail_overrides"]:
        print(f"Garde-fous : {report['guardrail_overrides']}")
    if report["ticket"]:
        print(f"Ticket     : n° {report['ticket']['ticket_id']} ({report['ticket']['status']})")
    print(f"Outils     : {[c['tool'] for c in report['tool_calls']]}")
    print(f"E-mails    : {[m['to'] for m in report['emails_sent']]}")
    if report["errors"]:
        print(f"Erreurs    : {report['errors'][0]}")
    print(f"Rapport    : {report.get('report_file')}")
    print("#" * 70)


def cmd_test(args):
    from agent.graph import process_email

    # Le mode test n'envoie jamais de vrai mail
    settings.dry_run = True

    if args.all:
        files = sorted(INCOMING_DIR.glob("*.json"))
    elif args.file:
        files = [Path(args.file)]
    else:
        files = []

    emails = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    if args.sender:
        emails.append({"sender": args.sender, "subject": args.subject or "", "body": args.body or ""})
    if not emails:
        sys.exit("Indiquez --file, --all, ou --sender/--subject/--body.")

    for i, email in enumerate(emails, 1):
        email.setdefault("message_id", f"<test-{i}@solvemate.local>")
        _print_summary(process_email(email))


def cmd_loop(args):
    from watcher import run_forever

    run_forever(args.interval)


def main():
    # Crée un analyseur des arguments pour exécuter SolveMate depuis le terminal
    parser = argparse.ArgumentParser(description="SolveMate : agent de support par e-mail")
    #action="store_true" signifie que args.verbose vaut True si tu ajoutes -v, sinon False
    parser.add_argument("-v", "--verbose", action="store_true")
    #Permet de définir plusieurs commandes. required=True signifie que tu dois obligatoirement choisir une commande.
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("index", help="Remplir Qdrant avec les procédures du PDF").set_defaults(func=cmd_index)

    p = sub.add_parser("test", help="Traiter un e-mail simulé (DRY_RUN forcé)")
    p.add_argument("--file", help="Fichier JSON {sender, subject, body}")
    p.add_argument("--all", action="store_true", help="Tous les JSON de incoming_emails/")
    p.add_argument("--sender")
    p.add_argument("--subject")
    p.add_argument("--body")
    p.set_defaults(func=cmd_test)
    #Cette commande lance la surveillance de ta boîte e-mail.
    p = sub.add_parser("loop", help="Surveiller la boîte de support")
    #Surveille les nouveaux e-mails avec 30 secondes d'attente entre deux vérifications.
    p.add_argument("--interval", type=int, default=None, help="Secondes entre deux vérifications")
    p.set_defaults(func=cmd_loop)

    args = parser.parse_args()
    _setup_logging(args.verbose)
    REPORTS_DIR.mkdir(exist_ok=True)
    try:
        #Il exécute la fonction correspondante selon le terminal 
        args.func(args)
    except KeyboardInterrupt:
        print("\nArrêt demandé.")


if __name__ == "__main__":
    main()
