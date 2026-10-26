"""Orchestration du traitement d'un e-mail.

Enchaînement explicite d'étapes (pas de LangGraph) :

    réception -> identification -> chargement du contexte -> raisonnement (LLM + outils)
              -> décision (validation + garde-fous) -> action -> trace

Pourquoi pas LangGraph : le flux est linéaire avec une seule branche (client inconnu) et une
seule boucle (tool calling). Le SDK google-genai gère nativement l'appel de fonctions ; une
boucle écrite à la main reste courte, sans dépendance LangChain, et permet de tracer chaque
appel d'outil dans les rapports.
"""
import json
import logging
import re
import time
#obtenir des informations détaillées sur une erreur : où elle s'est produite, dans quelle fonction et pourquoi
import traceback
from datetime import datetime
#Associé à Pydantic, il permet de vérifier automatiquement que les données reçues respectent ces valeurs.
from typing import Literal
from pydantic import BaseModel, Field, ValidationError, field_validator
from agent import tools
from agent.prompts import SYSTEM_PROMPT, build_user_message
from agent.tools import ToolBox
from config import REPORTS_DIR, settings
from mail_sender import reply_subject

logger = logging.getLogger(__name__)

MAX_TOOL_STEPS = 12    # nombre maximal d'allers-retours avec Gemini pour un e-mail
MAX_JSON_RETRIES = 2   # nombre de fois où l'on redemande le JSON s'il est mal formé

# Phrases typiques d'une tentative de manipulation de l'IA (« ignore tes règles », « tu es maintenant »...)
INJECTION_PATTERNS = re.compile(
    r"ignore[sz]?\s+(tes|vos|les|toutes|all|your|previous|the)\b.{0,30}(règles|regles|instructions|consignes|rules)"
    r"|oublie[sz]?\s+(tes|vos|les)\s+(règles|regles|instructions|consignes)"
    r"|(prompt\s+système|system\s+prompt)"
    r"|tu\s+es\s+(maintenant|désormais)|you\s+are\s+now"
    r"|nouvelles?\s+instructions?\s*:",
    re.IGNORECASE,
)


# =====================================================================
# Décision structurée (format de la réponse finale de Gemini)
# =====================================================================

class Decision(BaseModel):
    """La décision JSON attendue de Gemini. Pydantic vérifie chaque champ :
    Literal[...] = seules ces valeurs sont acceptées ; une valeur après « = » est celle par défaut."""
    category: Literal["Billing", "Network", "Service"]
    diagnosis: str
    facts_used: list[str] = Field(default_factory=list)   # liste vide par défaut
    decision: Literal["reply_to_customer", "escalate_to_operator"]
    confidence: float
    priority: Literal["Low", "Medium", "High"] = "Medium"
    customer_email_subject: str = ""
    customer_email_body: str = ""
    operator_email_body: str = ""
    memory_state: Literal["WaitingCustomer", "Escalated", "Resolved"]
    memory_summary: str = ""
    ticket_action: Literal["create", "update_existing", "none"] = "none"
    suspicious_content: bool = False

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v):
        """Ramène la confiance entre 0 et 1 (ex. 1.3 -> 1.0)."""
        return max(0.0, min(1.0, float(v)))


def parse_decision(text):
    """Extrait et valide l'objet JSON de la réponse du LLM."""
    # Enlève un éventuel bloc ```json ... ``` autour de la réponse
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    # Garde ce qui se trouve entre la première « { » et la dernière « } »
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Aucun objet JSON dans la réponse du LLM")
    # json.loads : texte -> dictionnaire ; model_validate : vérifie les champs avec Decision
    return Decision.model_validate(json.loads(cleaned[start : end + 1]))


# =====================================================================
# Raisonnement : boucle de tool calling Gemini
# =====================================================================

class GeminiReasoner:
    """Fait raisonner Gemini avec les outils, jusqu'à obtenir une décision JSON valide."""

    def __init__(self, api_key=None, model=None):
        from google import genai

        api_key = api_key or settings.gemini_api_key
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY n'est pas défini dans le fichier .env")
        self.client = genai.Client(api_key=api_key)   # connexion à l'API Gemini
        self.model = model or settings.gemini_model

    def _generate(self, contents, config, max_attempts=6):
        """Envoie la conversation à Gemini. Si Google répond « trop de requêtes » (429) ou a
        une panne (5xx), on attend puis on réessaie : 5 s, 10 s, 20 s... (60 s maximum)."""
        from google.genai import errors
        delay = 5.0
        for attempt in range(1, max_attempts + 1):
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except errors.APIError as exc:
                #429 :Too Many Requests
                #500:server
                retryable = exc.code == 429 or (exc.code or 0) >= 500
                if not retryable or attempt == max_attempts:
                    raise
                logger.warning("Gemini %s, nouvelle tentative dans %.0f s (%d/%d)",
                               exc.code, delay, attempt, max_attempts)
                time.sleep(delay)
                #À chaque erreur temporaire, on double le délai, sans jamais dépasser 60 secondes.
                delay = min(delay * 2, 60)

    def run(self, user_message, toolbox):
        """Conversation avec Gemini : il demande des outils (planification), on les exécute,
        on lui renvoie les résultats... jusqu'à sa décision finale en JSON.
        Retourne (décision, textes finaux de Gemini)."""
        from google.genai import types

        # Réglages envoyés à chaque appel : règles de l'agent, menu des outils, peu de créativité
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=ToolBox.declarations(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # on gère la boucle nous-mêmes
            temperature=0.1,
        )
        # Historique des échanges avec Gemini (messages, appels d'outils et résultats),
        # conservé pendant le traitement d'un e-mail pour que Gemini puisse utiliser
        # les informations des étapes précédentes sans les perdre.   
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_message)])]
        # Liste pour conserver les réponses textuelles finales de Gemini
        transcript = []
        # Compteur des tentatives de correction du JSON
        json_retries = 0

        for _ in range(MAX_TOOL_STEPS):
            response = self._generate(contents, config)
            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None:
                raise RuntimeError(f"Réponse vide du LLM : {response.prompt_feedback}")
            contents.append(candidate.content)   # on garde la réponse de Gemini dans l'historique

            # Cas 1 : Gemini demande un ou plusieurs outils -> on les exécute et on lui renvoie les résultats
            if response.function_calls:
                parts = []
                for fc in response.function_calls:
                    result = toolbox.call(fc.name, dict(fc.args or {}))
                    parts.append(
                        #Transforme le résultat de l'outil (demander par gemini) en un format compréhensible par Gemini
                        types.Part.from_function_response(
                            name=fc.name, response=ToolBox.to_response(result)
                        )
                    )
                contents.append(types.Content(role="user", parts=parts))
                continue   # tour suivant : Gemini lit les résultats

            # Cas 2 : Gemini a fini -> sa réponse doit être la décision JSON
            text = response.text or ""
            transcript.append(text)
            try:
                return parse_decision(text), transcript
            except (ValueError, ValidationError, json.JSONDecodeError) as exc:
                # JSON mal formé : on lui redemande (au plus MAX_JSON_RETRIES fois)
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    raise RuntimeError(f"Décision JSON invalide : {exc}") from exc
                contents.append(types.Content(role="user", parts=[types.Part.from_text(
                    text="Ta réponse n'est pas un JSON valide conforme au format demandé "
                    f"({exc}). Réponds UNIQUEMENT avec l'objet JSON de décision."
                )]))

        raise RuntimeError("Nombre maximal d'étapes de raisonnement atteint sans décision")


# =====================================================================
# Traitement d'un e-mail
# =====================================================================

class EmailCase:
    """Le dossier d'un e-mail : tout ce qui se passe pendant le traitement.
    À la fin, il est enregistré dans reports/ (le rapport)."""

    def __init__(self, email):
        self.email = email
        self.started_at = datetime.now()
        self.steps = []            # étapes passées (identification, raisonnement, ticket...)
        self.customer = None       # le client trouvé (None si inconnu)
        self.open_tickets = []
        self.memory = []
        self.toolbox = None        # les outils de ce client (voir agent/tools.py)
        self.llm_transcript = []   # textes finaux de Gemini
        self.decision = None       # la décision validée (Decision)
        self.overrides = []        # garde-fous qui ont modifié la décision
        self.ticket = None
        self.emails_sent = []
        self.memory_saved = None
        self.errors = []
        self.outcome = ""          # résultat : replied, escalated, unknown_customer ou error

    def step(self, name, **details):
        """Note une étape (avec l'heure) et l'affiche dans le terminal."""
        self.steps.append({"step": name, "at": datetime.now().isoformat(timespec="seconds"), **details})
        logger.info("[%s] %s", self.email.get("sender"), name)

    def report(self):
        """Le rapport complet du dossier (enregistré en JSON dans reports/)."""
        return {
            "received_email": self.email,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "outcome": self.outcome,
            "steps": self.steps,
            "customer": self.customer,
            "context": {"open_tickets": self.open_tickets, "memory": self.memory},
            "tool_calls": self.toolbox.calls if self.toolbox else [],
            "rag_sections_used": self.toolbox.rag_sections if self.toolbox else [],
            "llm_final_text": self.llm_transcript,
            "decision": self.decision.model_dump() if self.decision else None,
            "guardrail_overrides": self.overrides,
            "ticket": self.ticket,
            "emails_sent": self.emails_sent,
            "memory_saved": self.memory_saved,
            "errors": self.errors,
        }


def process_email(email, reasoner=None, write_report=True):
    """Traite un e-mail de bout en bout.
    `email` : {"sender", "subject", "body", "date"?, "message_id"?, "in_reply_to"?, "references"?}
    `reasoner` : Gemini par défaut (les tests le remplacent par un faux).
    """
    # Crée un dossier pour conserver toutes les informations du traitement

    case = EmailCase(email)
    # Enregistre l'étape de réception avec l'objet de l'e-mail
    case.step("reception", subject=email.get("subject"))
    try:
        # Identifie le client à partir de son adresse e-mail
        _identify(case)
        if case.customer is None:
            _handle_unknown_customer(case)
        else:
            # Récupère les tickets ouverts et la mémoire du client
            _load_context(case)
            # Gemini analyse la demande avec les outils et retourne une décision
            _reason(case, reasoner or GeminiReasoner())
            # Vérifie la décision et applique les règles de sécurité
            _apply_guardrails(case)
            # Exécute la décision : tickets, e-mails et mémoire
            _act(case)
    except Exception as exc:
        # Toute erreur est transmise à l'opérateur au lieu d'arrêter le programme
        _handle_failure(case, exc)
    # Enregistre le rapport dans un fichier JSON si demandé
    report = case.report()
    if write_report:
        report["report_file"] = str(_write_report(report))
    return report


# ---------------------- Étapes ----------------------

def _identify(case):
    """Retrouve le client grâce à l'adresse de l'expéditeur."""
    customer = tools.get_customer(case.email["sender"])
    case.customer = customer if "customer_id" in customer else None
    case.step("identification", found=case.customer is not None,
              customer_id=case.customer["customer_id"] if case.customer else None)


def _load_context(case):
    """Charge les tickets ouverts et la mémoire du client (donnés à Gemini dès le départ)."""
    cid = case.customer["customer_id"]
    case.open_tickets = tools.get_open_tickets(cid)
    case.memory = tools.get_memory(cid)
    case.step("chargement_contexte", open_tickets=len(case.open_tickets), memory=len(case.memory))


def _reason(case, reasoner):
    """Gemini raisonne avec les outils du client et rend sa décision."""
    case.toolbox = ToolBox(case.customer)
    message = build_user_message(case.email, case.customer, case.open_tickets, case.memory)
    case.decision, case.llm_transcript = reasoner.run(message, case.toolbox)
    case.step("raisonnement", tools_called=[c["tool"] for c in case.toolbox.calls])


def _apply_guardrails(case):
    """Règles imposées par l'application, quelle que soit la réponse du LLM."""
    d = case.decision
    # 1. Confiance trop faible -> escalade
    if d.decision == "reply_to_customer" and d.confidence < settings.confidence_threshold:
        case.overrides.append(
            f"confiance {d.confidence:.2f} < {settings.confidence_threshold} : escalade forcée"
        )
        d.decision = "escalate_to_operator"

    # 2. E-mail suspect (détecté par Gemini ou par nos phrases types) -> escalade
    injected = bool(INJECTION_PATTERNS.search(f"{case.email.get('subject', '')}\n{case.email.get('body', '')}"))
    if d.suspicious_content or injected:
        d.suspicious_content = True
        if d.decision != "escalate_to_operator":
            case.overrides.append("contenu suspect (tentative d'instruction dans l'e-mail) : escalade forcée")
            d.decision = "escalate_to_operator"

    # 3. Gemini veut répondre mais le texte est vide -> escalade
    if d.decision == "reply_to_customer" and not d.customer_email_body.strip():
        case.overrides.append("réponse au client vide : escalade forcée")
        d.decision = "escalate_to_operator"

    # 4. Une escalade est toujours enregistrée en mémoire avec l'état Escalated
    if d.decision == "escalate_to_operator" and d.memory_state != "Escalated":
        d.memory_state = "Escalated"

    case.step("decision", decision=d.decision, confidence=d.confidence, overrides=case.overrides)


def _act(case):
    """Exécute la décision : ticket, e-mails, puis mémoire."""
    d, customer, email = case.decision, case.customer, case.email
    cid = customer["customer_id"]
    escalate = d.decision == "escalate_to_operator"

    # --- Ticket : jamais de doublon (create_ticket renvoie le ticket ouvert existant) ---
    same_category = next((t for t in case.open_tickets if t["category"] == d.category), None)
    if escalate or d.ticket_action in ("create", "update_existing"):
        result = tools.create_ticket(cid, d.category, d.priority, f"[SolveMate] {d.diagnosis}")
        case.ticket = result["ticket"]
        if not result["created"]:
            # Ticket déjà ouvert : on y ajoute une note (et il passe « In Progress » en cas d'escalade)
            new_status = "In Progress" if escalate else case.ticket["status"]
            note = f"[SolveMate {datetime.now():%Y-%m-%d %H:%M}] Nouveau mail « {email.get('subject', '')} ». {d.diagnosis}"
            case.ticket = tools.update_ticket(case.ticket["ticket_id"], new_status, note, customer_id=cid)["ticket"]
        case.step("ticket", created=result["created"], ticket_id=case.ticket["ticket_id"])
    elif same_category:
        # Pas d'action demandée, mais un ticket de même catégorie existe : on le relie au dossier
        case.ticket = same_category
        case.step("ticket", created=False, ticket_id=same_category["ticket_id"], note="ticket existant lié")

    # --- E-mails (in_reply_to / references : la réponse reste dans le même fil de discussion) ---
    thread = {"in_reply_to": email.get("message_id"), "references": email.get("references")}
    customer_subject = reply_subject(email.get("subject") or d.customer_email_subject)

    if escalate:
        # Dossier complet à l'opérateur + accusé de réception au client
        #Première action : envoyer le dossier à l'opérateur
        operator_body = _operator_dossier(case)
        case.emails_sent.append(tools.escalate_to_operator(
            cid, f"{d.category} / priorité {d.priority} : {email.get('subject', '')}", operator_body))
        #Deuxième action : informer le client
        forced = bool(case.overrides)
        ack = d.customer_email_body.strip() if (d.customer_email_body.strip() and not forced) else _generic_ack(customer)
        case.emails_sent.append(tools.send_email_to_customer(customer["email"], customer_subject, ack, **thread))
    else:
        case.emails_sent.append(tools.send_email_to_customer(
            customer["email"], customer_subject, d.customer_email_body.strip(), **thread))
    case.step("emails", count=len(case.emails_sent))

    # --- Mémoire : un souvenir de ce qui a été fait, pour le prochain e-mail du client ---
    action = "Escalade à l'opérateur" if escalate else "Réponse envoyée au client"
    case.memory_saved = tools.save_memory(
        cid,
        case.ticket["ticket_id"] if case.ticket else None,
        email.get("subject", ""),
        d.memory_summary or d.diagnosis,
        action,
        d.memory_state,
    )
    case.outcome = "escalated" if escalate else "replied"
    case.step("memoire", state=d.memory_state)


# ---------------------- Cas particuliers ----------------------

UNKNOWN_CUSTOMER_REPLY = """\
Bonjour,

Nous avons bien reçu votre message, mais nous ne retrouvons pas de contrat associé à cette adresse e-mail.
Votre demande a été transmise à un conseiller, qui va vérifier votre identité et revenir vers vous.

Cordialement,
Le service client SolveMate

---

Hello,

We received your message but could not find a contract linked to this e-mail address.
Your request has been forwarded to an advisor, who will verify your identity and get back to you.

Kind regards,
SolveMate Customer Service
"""


def _handle_unknown_customer(case):
    """Expéditeur inconnu : on transmet le dossier à l'opérateur, qui vérifiera l'identité.
    Aucune donnée n'est consultée et Gemini n'est pas appelé."""
    email = case.email
    # Si l'e-mail répond à un message précédent (en-tête In-Reply-To), le client a déjà reçu
    # notre réponse automatique : on ne la renvoie pas, sinon il recevrait le même message en boucle
    already_answered = bool(email.get("in_reply_to"))
    body = (
        "Client inconnu : aucun client ne correspond à l'adresse de l'expéditeur.\n"
        "Aucune autre donnée n'a été consultée.\n\n"
        f"Expéditeur : {email.get('sender')}\nDate : {email.get('date', '')}\n"
        f"Sujet : {email.get('subject', '')}\n\nMessage reçu :\n{email.get('body', '')}\n\n"
        "Action suggérée : vérifier l'identité du client, puis enregistrer son adresse e-mail "
        "dans sa fiche client pour que ses prochains messages soient traités automatiquement.\n"
        "Priorité : Medium"
    )
    case.emails_sent.append(tools.escalate_to_operator(None, f"Client inconnu : {email.get('subject', '')}", body))
    if not already_answered:
        case.emails_sent.append(tools.send_email_to_customer(
            email["sender"], reply_subject(email.get("subject", "")), UNKNOWN_CUSTOMER_REPLY,
            in_reply_to=email.get("message_id"), references=email.get("references")))
    case.outcome = "unknown_customer"
    case.step("client_inconnu", emails=len(case.emails_sent))


def _handle_failure(case, exc):
    """Erreur pendant le traitement : on la note et on transmet le dossier à l'opérateur."""
    case.errors.append(f"{type(exc).__name__}: {exc}")
    case.errors.append(traceback.format_exc())
    case.outcome = "error"
    logger.exception("Échec du traitement de l'e-mail de %s", case.email.get("sender"))
    email = case.email
    body = (
        "Traitement automatique impossible. Merci de prendre ce dossier en charge manuellement.\n\n"
        f"Erreur : {type(exc).__name__}: {exc}\n\n"
        f"Client : {json.dumps(case.customer, ensure_ascii=False, default=str) if case.customer else 'non identifié'}\n"
        f"Expéditeur : {email.get('sender')}\nSujet : {email.get('subject', '')}\n\n"
        f"Message reçu :\n{email.get('body', '')}"
    )
    try:
        cid = case.customer["customer_id"] if case.customer else None
        case.emails_sent.append(tools.escalate_to_operator(cid, "Traitement automatique impossible", body))
    except Exception as mail_exc:
        case.errors.append(f"Escalade impossible : {type(mail_exc).__name__}: {mail_exc}")
    case.step("erreur", error=str(exc))


def _generic_ack(customer):
    """Accusé de réception générique, utilisé quand un garde-fou a forcé l'escalade."""
    name = customer.get('name') or ''
    # Texte fixe : on écrit les deux langues (français puis anglais)
    return (
        f"Bonjour {name},\n\n"
        "Nous avons bien reçu votre message.\n\n"
        "Votre dossier a été transmis à un conseiller, qui reviendra vers vous rapidement.\n\n"
        "Cordialement,\nLe service client SolveMate\n\n"
        "---\n\n"
        f"Hello {name},\n\n"
        "We have received your message.\n\n"
        "Your request has been forwarded to an advisor, who will get back to you shortly.\n\n"
        "Kind regards,\nSolveMate Customer Service"
    )


def _operator_dossier(case):
    """Dossier opérateur : texte du LLM + annexe factuelle générée par l'application."""
    d, c, email = case.decision, case.customer, case.email

    def section(title, data):
        return f"\n## {title}\n{json.dumps(data, ensure_ascii=False, indent=1, default=str)}\n"

    # Les données lues par les outils (sauf la recherche dans les procédures)
    tool_data = {call["tool"]: call["result"] for call in (case.toolbox.calls if case.toolbox else [])
                 if call["tool"] != "search_knowledge"}
    parts = [
        d.operator_email_body.strip() or "(pas de texte rédigé par l'agent)",
        "\n" + "=" * 60 + "\nANNEXE GÉNÉRÉE PAR L'APPLICATION",
        f"\nClient : {c.get('name')} (n° {c['customer_id']}), {c.get('email')}, "
        f"abonnement {c.get('subscription_type')}, zone {c.get('network_zone')}",
        f"Catégorie : {d.category} | Priorité : {d.priority} | Confiance : {d.confidence:.2f}",
        f"Hypothèse de l'agent : {d.diagnosis}",
        "Faits vérifiés :\n" + "\n".join(f"  - {f}" for f in d.facts_used),
    ]
    if case.overrides:
        parts.append("Garde-fous appliqués :\n" + "\n".join(f"  - {o}" for o in case.overrides))
    if d.suspicious_content:
        parts.append("ATTENTION : l'e-mail contient des consignes adressées à l'agent ; elles ont été ignorées.")
    if case.ticket:
        parts.append(f"Ticket : n° {case.ticket['ticket_id']} ({case.ticket['category']}, {case.ticket['status']})")
    parts.append(f"\n## E-mail reçu\nSujet : {email.get('subject', '')}\n{email.get('body', '')}")
    parts.append(section("Données consultées", tool_data))
    parts.append(section("Tickets ouverts / mémoire", {"tickets": case.open_tickets, "memory": case.memory}))
    return "\n".join(parts)


def _write_report(report):
    """Enregistre le rapport dans reports/ (nom = date + heure + expéditeur) et renvoie son chemin."""
    REPORTS_DIR.mkdir(exist_ok=True)
    sender = re.sub(r"[^A-Za-z0-9._-]+", "_", report["received_email"].get("sender", "inconnu"))
    path = REPORTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S-%f}_{sender}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
