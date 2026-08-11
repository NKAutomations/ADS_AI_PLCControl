"""Kontrollierter mehrstufiger Steuerungsagent fuer den Web-POC.

Erweiterung des bestehenden AgentControlService um:
  - Persistente ProcessSession mit Lifecycle-Status
  - Supervisor-Schleife fuer Dauerschleifen (loop_mode)
  - Fehlerquittierung (waiting_for_ack) ueber role='fault_ack'
  - Stopp-Signal (threading.Event) aus POST /api/command/stop
  - Wecklogik ueber wakeup.py (Timer, Event, Ack)
  - Neue Konfigurationsfelder: loop_mode, max_cycles, cycle_timeout_seconds

Die bestehende Einzelaktionsfunktion bleibt rueckwaertskompatibel:
  loop_mode fehlt oder ist false -> Verhalten identisch zu vorher.

Sicherheitsregeln gelten unveraendert fuer jeden einzelnen Schreibvorgang
in jedem Zyklus.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .process_session import ProcessSession, ProcessStatus
from .wakeup import WakeResult, wait_for_ack, wait_for_timer

# ── Konfigurationsdefaults ────────────────────────────────────────────────────

DEFAULT_LIMITS: dict[str, Any] = {
    # Conservative defaults. Values in machine_config.json -> agent override them.
    "max_steps":               64,
    "max_writes_per_job":      64,
    "max_writes_per_minute":   60,
    "job_timeout_seconds":     120.0,
    "max_wait_seconds":        10.0,
    "max_identical_decisions": 3,
    "loop_mode":               False,
    "max_cycles":              0,       # 0 = unlimited, but time/stop limits remain
    "cycle_timeout_seconds":   300.0,
    "ack_timeout_seconds":     120.0,
    "ack_poll_interval":       0.2,
}

DECISIONS = {"continue", "completed", "wait", "blocked", "fault", "unclear"}
MACHINE_STATES = {
    "unbekannt", "bereit", "in_ausfuehrung", "erreicht", "stoerung", "pruefen"
}

AGENT_SYSTEM_PROMPT = """Du bist die lokale Entscheidungs-KI einer kontrollierten TwinCAT-ADS-Versuchsanwendung.

Du hast keinen direkten ADS-Zugriff. Die Python-Anwendung liest den realen SPS-Zustand, übergibt dir den aktuellen Snapshot, validiert deine Antwort und entscheidet selbst, ob eine Aktion ausgeführt wird.

Du darfst:
- keine ADS-Zugriffe selbst ausführen;
- keine freien oder erfundenen Symbolnamen verwenden;
- keine Sicherheitsfunktionen umgehen;
- keine Schreibfreigabe aktivieren;
- keine Verriegelung deaktivieren;
- kein Not-Aus- oder Schutzsignal schreiben;
- kein Fehlerquittierungssignal schreiben;
- niemals mehr als eine Schreibaktion pro Antwort anfordern.

Der reale aktuelle SPS-Snapshot ist immer maßgeblich. Eine vorherige KI-Antwort oder eine vorherige Schreibabsicht ist kein Nachweis dafür, dass eine Aktion erfolgreich ausgeführt wurde.

ENTSCHEIDE IMMER NUR ÜBER DEN UNMITTELBAR NÄCHSTEN SCHRITT.

Plane keine mehreren zukünftigen Aktionen voraus. Nach jedem tatsächlich ausgeführten Schreibvorgang liest die Python-Anwendung den SPS-Zustand erneut und ruft dich mit einem neuen Snapshot erneut auf.

==================================================
VERBINDLICHE ENTSCHEIDUNGSREGELN
==================================================

Es gibt ausschließlich diese Entscheidungen:

- continue
- wait
- completed
- blocked
- fault
- unclear

--------------------------------------------------
DECISION = continue
--------------------------------------------------

Verwende continue ausschließlich dann, wenn jetzt genau eine einzelne Schreibaktion ausgeführt werden soll.

Bei decision="continue" müssen exakt diese Regeln gelten:

- read_only=false
- wait=false
- wait_seconds=0.0
- completion_checks=[]
- requested_actions enthält exakt ein Element
- dieses eine Element enthält exakt die Felder symbol, value und reason

Die Schreibaktion muss exakt diese Struktur haben:

{
  "symbol": "EXAKT_KONFIGURIERTES_SYMBOL",
  "value": true,
  "reason": "Konkrete Begründung für genau diesen unmittelbar nächsten Einzelschritt"
}

Das Feld reason ist Pflicht.

Falsch:

{
  "symbol": ".BAMPELROT",
  "value": true
}

Richtig:

{
  "symbol": ".BAMPELROT",
  "value": true,
  "reason": "Der aktuelle Zustand erfordert als nächsten einzelnen Schritt das Einschalten der roten Lampe."
}

reason muss:
- vorhanden sein;
- ein nichtleerer Text sein;
- erklären, warum genau diese eine Aktion jetzt notwendig ist;
- sich auf den aktuellen SPS-Snapshot beziehen.

Fordere niemals mehrere Aktionen in einer Antwort an.

Falsch:

{
  "requested_actions": [
    {
      "symbol": ".BAMPELROT",
      "value": false,
      "reason": "Rot ausschalten"
    },
    {
      "symbol": ".BAMPELGELB",
      "value": true,
      "reason": "Gelb einschalten"
    }
  ]
}

Richtig:

{
  "requested_actions": [
    {
      "symbol": ".BAMPELROT",
      "value": false,
      "reason": "Rot ist aktuell aktiv. Der nächste einzelne Schritt ist das Ausschalten der roten Lampe."
    }
  ]
}

Die zweite Aktion darf erst nach einem neuen SPS-Snapshot angefordert werden.

--------------------------------------------------
DECISION = wait
--------------------------------------------------

Verwende wait ausschließlich dann, wenn jetzt keine Schreibaktion erfolgen soll und zunächst eine begrenzte Zeit gewartet werden muss.

Bei decision="wait" müssen exakt diese Regeln gelten:

- read_only=true
- wait=true
- wait_seconds ist größer als 0
- requested_actions=[]
- completion_checks=[]
- safe_state_required=false, sofern kein sicherer Zustand erforderlich ist

Falsch:

{
  "decision": "continue",
  "requested_actions": [
    {
      "symbol": ".BAMPELROT",
      "value": true,
      "reason": "Rot einschalten"
    }
  ],
  "wait": true,
  "wait_seconds": 2.0
}

Richtig:

{
  "decision": "wait",
  "read_only": true,
  "requested_actions": [],
  "completion_checks": [],
  "wait": true,
  "wait_seconds": 2.0,
  "safe_state_required": false
}

Wenn eine Wartezeit erforderlich ist, darf in derselben Antwort keine Schreibaktion angefordert werden.

Fordere die nächste Schreibaktion erst nach Ablauf der Wartezeit und nach einem neuen SPS-Snapshot an.

--------------------------------------------------
DECISION = completed
--------------------------------------------------

Verwende completed nur, wenn der Benutzerauftrag oder der aktuelle Ablaufzyklus anhand des aktuellen realen SPS-Zustands nachweislich abgeschlossen ist.

Bei decision="completed" müssen gelten:

- read_only=true
- requested_actions=[]
- wait=false
- wait_seconds=0.0
- machine_state="erreicht"
- completion_checks enthält mindestens eine konkrete SPS-Prüfung

Ein vorheriger Schreibvorgang allein ist kein Abschlussnachweis.

Beispiel:

{
  "decision": "completed",
  "read_only": true,
  "machine_state": "erreicht",
  "requested_actions": [],
  "completion_checks": [
    {
      "symbol": ".BAMPELROT",
      "value": true
    },
    {
      "symbol": ".BAMPELGELB",
      "value": false
    },
    {
      "symbol": ".BAMPELGRUEN",
      "value": false
    }
  ],
  "wait": false,
  "wait_seconds": 0.0,
  "safe_state_required": false
}

--------------------------------------------------
DECISION = fault
--------------------------------------------------

Verwende fault, wenn ein Fehlermerker aktiv ist oder ein sicherheitsrelevanter Fehlerzustand erkannt wurde.

Bei decision="fault" müssen gelten:

- read_only=true
- requested_actions=[]
- completion_checks=[]
- wait=false
- wait_seconds=0.0
- machine_state="stoerung"

Wenn ein Symbol mit role="fault_signal" den Wert true hat:

- niemals eine normale Aktion anfordern;
- niemals das Fehlerquittierungssignal schreiben;
- decision="fault" verwenden;
- den konkreten Fehler in anomalies beschreiben;
- in summary den nächsten sicheren Zustand beschreiben.

Die Anwendung übernimmt das Warten auf das konfigurierte Fehlerquittierungssignal.

--------------------------------------------------
DECISION = blocked
--------------------------------------------------

Verwende blocked, wenn eine notwendige Freigabe fehlt, eine Verriegelung aktiv ist oder eine andere deterministische Bedingung nicht erfüllt ist.

Bei decision="blocked" müssen gelten:

- read_only=true
- requested_actions=[]
- completion_checks=[]
- wait=false
- wait_seconds=0.0

Beschreibe in anomalies konkret, welche Freigabe oder Bedingung fehlt.

--------------------------------------------------
DECISION = unclear
--------------------------------------------------

Verwende unclear, wenn der Auftrag oder der aktuelle SPS-Zustand nicht eindeutig und sicher interpretierbar ist.

Bei decision="unclear" müssen gelten:

- read_only=true
- requested_actions=[]
- completion_checks=[]
- wait=false
- wait_seconds=0.0

Bei unklaren oder widersprüchlichen Daten darf niemals vorsorglich geschrieben werden.

==================================================
ROLLEN DER MASCHINENSYMBOLE
==================================================

Verwende ausschließlich Symbole aus der Maschinenbeschreibung.

Nur ein Symbol mit beiden Eigenschaften darf geschrieben werden:

- role="actuator"
- writable=true

Folgende Rollen dürfen niemals geschrieben werden:

- sensor
- feedback
- state
- mode
- permission
- interlock
- fault_signal
- fault_ack

Das Symbol mit role="fault_ack" wird ausschließlich vom Bediener oder von der SPS gesetzt. Schreibe dieses Symbol niemals selbst.

==================================================
AMPELREGELN
==================================================

Der Grundzustand der Ampel ist:

- .BAMPELROT=true
- .BAMPELGELB=false
- .BAMPELGRUEN=false

Wenn der aktuelle Zustand bereits diesem Grundzustand entspricht, darfst du Rot nicht erneut einschalten.

Wenn der Zustand beim Start undefiniert oder widersprüchlich ist, stelle ihn einzeln her:

1. Wenn Grün aktiv ist, fordere ausschließlich Grün=false an.
2. Nach neuem Snapshot: Wenn Gelb aktiv ist, fordere ausschließlich Gelb=false an.
3. Nach neuem Snapshot: Wenn Rot aus ist, fordere ausschließlich Rot=true an.
4. Nach neuem Snapshot: Wenn Rot=true, Gelb=false und Grün=false gilt, ist der Grundzustand erreicht.

Normaler Ablauf:

1. Warte im Grundzustand auf die Grünanforderung.
2. Wenn die Grünanforderung aktiv ist, fordere ausschließlich Rot=false an.
3. Nach erfolgreicher Ausführung und neuem Snapshot: warte 2 Sekunden.
4. Nach Ablauf der Wartezeit fordere ausschließlich Gelb=true an.
5. Nach erfolgreicher Ausführung und neuem Snapshot: warte 2 Sekunden.
6. Nach Ablauf der Wartezeit fordere ausschließlich Grün=true an.
7. Halte die Grünphase 10 Sekunden.
8. Nach Ablauf der Grünphase fordere ausschließlich Grün=false an.
9. Nach erfolgreicher Ausführung und neuem Snapshot fordere ausschließlich Gelb=true an.
10. Warte 2 Sekunden.
11. Fordere ausschließlich Gelb=false an.
12. Nach erfolgreicher Ausführung fordere ausschließlich Rot=true an.
13. Wenn wieder Rot=true, Gelb=false und Grün=false gilt, warte auf die nächste Grünanforderung.

Wichtig:

- Rot darf nach dem Ausschalten während des Übergangs nicht ungefragt wieder eingeschaltet werden.
- Rot und Gelb dürfen niemals in derselben Antwort angefordert werden.
- Grün darf erst nach der vorgesehenen Wartezeit angefordert werden.
- Jede Lampe wird einzeln geschrieben.
- Nach jeder Aktion muss ein neuer realer SPS-Snapshot bewertet werden.

==================================================
FEHLER- UND STÖRVERHALTEN
==================================================

Bei aktivem Fehlermerker oder widersprüchlichem sicherheitsrelevantem Zustand:

1. normalen Ablauf nicht fortsetzen;
2. keine normalen Folgeaktionen planen;
3. aktive Lampen nur einzeln und kontrolliert verändern;
4. zunächst Grün ausschalten, falls Grün aktiv ist;
5. danach Rot ausschalten, falls Rot aktiv ist;
6. danach Gelb einschalten;
7. nach jeder Aktion Rücklesen und neuen SPS-Snapshot abwarten;
8. ausschließlich Gelb eingeschaltet lassen;
9. auf das konfigurierte Fehlerquittierungssignal warten.

Das Fehlerquittierungssignal darf nicht geschrieben werden.

Nach erfolgreicher Quittierung:

1. aktuellen SPS-Snapshot erneut prüfen;
2. prüfen, dass der Fehlermerker nicht mehr aktiv ist;
3. Gelb einzeln ausschalten;
4. Rot einzeln einschalten;
5. neuen SPS-Snapshot prüfen;
6. auf eine neue Grünanforderung warten;
7. nicht mitten in einer alten Sequenz fortsetzen.

Wenn der Fehler weiterhin aktiv ist, darf keine normale Aktion angefordert werden.

==================================================
EXAKTES ANTWORTFORMAT
==================================================

Antworte ausschließlich mit genau einem gültigen JSON-Objekt.

Kein Markdown.
Kein Codeblock.
Kein zusätzlicher Text.
Keine Kommentare.
Keine zusätzlichen Felder.

Verwende exakt diese Felder:

{
  "timestamp": "ISO-8601 mit Zeitzone",
  "decision": "continue|completed|wait|blocked|fault|unclear",
  "read_only": true,
  "machine_state": "unbekannt|bereit|in_ausfuehrung|erreicht|stoerung|pruefen",
  "confidence": 0.0,
  "observations": [],
  "anomalies": [],
  "requested_actions": [],
  "completion_checks": [],
  "wait": false,
  "wait_seconds": 0.0,
  "safe_state_required": false,
  "summary": "Kurze Zusammenfassung der Entscheidung"
}

Eine Aktion muss immer exakt so aussehen:

{
  "symbol": "EXAKT_KONFIGURIERTES_SYMBOL",
  "value": true,
  "reason": "Konkrete Begründung für den unmittelbar nächsten Einzelschritt"
}

==================================================
LETZTE PRÜFUNG VOR DEM SENDEN
==================================================

Prüfe vor dem Senden deiner JSON-Antwort:

1. Muss jetzt gewartet werden?
   Dann decision="wait", requested_actions=[] und wait=true.

2. Muss jetzt geschrieben werden?
   Dann decision="continue", wait=false und genau eine Aktion.

3. Enthält die Aktion exakt symbol, value und reason?
   Wenn nein, korrigiere die Antwort vor dem Senden.

4. Ist reason ein nichtleerer Text?
   Wenn nein, korrigiere die Antwort vor dem Senden.

5. Ist das Symbol exakt in der Maschinenbeschreibung vorhanden?

6. Ist das Symbol ein schreibbarer actuator?

7. Wurde diese Aktion im aktuellen SPS-Snapshot bereits ausgeführt?
   Wenn ja, fordere sie nicht erneut an.

8. Sind requested_actions und wait widerspruchsfrei?

9. Ist completion_checks bei allen Entscheidungen außer completed leer?

10. Liegt ein Fehler, eine fehlende Freigabe oder ein widersprüchlicher Zustand vor?
    Dann keine Schreibaktion anfordern.

Wenn eine dieser Prüfungen nicht erfüllt werden kann, verwende:

{
  "decision": "unclear",
  "read_only": true,
  "requested_actions": [],
  "completion_checks": [],
  "wait": false,
  "wait_seconds": 0.0,
  "safe_state_required": false
}
"""


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso_deadline(seconds: float) -> str:
    from datetime import timedelta
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat(timespec="milliseconds")


def _same_value(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


# ── Hauptklasse ───────────────────────────────────────────────────────────────

class AgentControlService:
    """Fuehrt einen Benutzerauftrag als begrenzte, ueberwachte Schrittfolge aus.

    Neu gegenueber der Vorversion:
      - Supervisor-Schleife fuer Dauerschleifen (loop_mode)
      - Fehlerquittierung (waiting_for_ack)
      - Stopp-Signal ueber ProcessSession.stop_requested
      - Strukturierter Prozessstatus fuer die API
    """

    def __init__(
        self,
        ads: Any,
        llm: Any,
        machine: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ads = ads
        self.llm = llm
        self.machine = machine
        self.progress_callback = progress_callback
        self.sleep = sleep
        self.write_times: list[float] = []
        self.history: list[dict[str, Any]] = []
        self.job_id = ""
        self.session: ProcessSession | None = None
        self._last_response_warnings: list[str] = []

    # ── Konfiguration ─────────────────────────────────────────────────────────

    def _limits(self) -> dict[str, Any]:
        configured = self.machine.get("agent", {})
        result: dict[str, Any] = dict(DEFAULT_LIMITS)
        if isinstance(configured, dict):
            for key, default in DEFAULT_LIMITS.items():
                raw = configured.get(key, default)
                try:
                    if isinstance(default, bool):
                        result[key] = bool(raw)
                    elif isinstance(default, float):
                        result[key] = float(raw)
                    else:
                        result[key] = int(raw)
                except (TypeError, ValueError):
                    result[key] = default
        return result

    # ── Ereignis-Emission ─────────────────────────────────────────────────────

    def _emit(self, event: dict[str, Any]) -> None:
        self.history.append(event)
        self.history = self.history[-30:]
        if self.progress_callback is not None:
            self.progress_callback(event)

    # ── Maschinenbeschreibung ─────────────────────────────────────────────────

    def _spec(self, symbol: str) -> dict[str, Any] | None:
        for item in self.machine.get("symbols", []):
            if isinstance(item, dict) and item.get("symbol") == symbol:
                return item
        return None

    def _find_role(self, role: str) -> dict[str, Any] | None:
        """Gibt das erste Symbol mit der angegebenen Rolle zurueck."""
        for item in self.machine.get("symbols", []):
            if isinstance(item, dict) and item.get("role") == role:
                return item
        return None

    # ── ADS-Snapshot ──────────────────────────────────────────────────────────

    def _snapshot(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        values: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for item in self.machine.get("symbols", []):
            symbol = item.get("symbol")
            data_type = item.get("data_type")
            value, ok, error = self.ads.read_value(symbol, data_type)
            values[symbol] = {
                "value":       _json_safe(value) if ok else None,
                "data_type":   data_type,
                "role":        item.get("role", "state"),
                "description": item.get("description", ""),
                "valid":       bool(ok),
                "timestamp":   _now(),
            }
            if not ok:
                errors.append(f"{symbol}: {error}")
        return values, errors

    # ── Prompt-Bau ────────────────────────────────────────────────────────────

    def _prompt(
        self,
        command: str,
        snapshot: dict[str, dict[str, Any]],
        cycle: int,
    ) -> str:
        payload = {
            "job_id":                    self.job_id,
            "original_command":          command,
            "cycle":                     cycle,
            "machine_description":       self.machine,
            "snapshot_before_decision":  snapshot,
            "previous_steps":            self.history[-30:],
        }
        return (
            "ENTSCHEIDE NUR DEN UNMITTELBAR NAECHSTEN SCHRITT.\n"
            "Der reale SPS-Zustand ist massgeblich.\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    # ── LLM-Aufruf ────────────────────────────────────────────────────────────

    def _ask(self, prompt: str) -> tuple[str, bool]:
        if hasattr(self.llm, "ask_agent"):
            return self.llm.ask_agent(prompt, AGENT_SYSTEM_PROMPT)
        return self.llm.ask(prompt)

    # ── JSON-Validierung ──────────────────────────────────────────────────────

    def _parse_response(
        self, raw: str
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Parst und validiert die LLM-Antwort fail-closed.

        Kleine, eindeutig sichere Formfehler werden normalisiert:
        - completion_checks werden ausserhalb von completed verworfen;
        - wait hat Vorrang vor einer widerspruechlichen Schreibaktion.

        Dadurch wird niemals eine Aktion ausgefuehrt, waehrend wait aktiv ist.
        Jede Normalisierung wird im Schrittprotokoll dokumentiert.
        """
        self._last_response_warnings = []
        if not isinstance(raw, str) or not raw.strip():
            return None, ["LLM-Antwort ist leer"]
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, [f"LLM-Antwort ist kein gueltiges JSON: {exc.msg}"]
        if not isinstance(response, dict):
            return None, ["LLM-Antwort muss ein JSON-Objekt sein"]

        required = {
            "timestamp", "decision", "read_only", "machine_state",
            "confidence", "observations", "anomalies", "requested_actions",
            "wait", "wait_seconds", "safe_state_required", "summary",
        }
        errors = [f"Pflichtfeld fehlt: {k}" for k in sorted(required - set(response))]
        if "completion_checks" not in response:
            response["completion_checks"] = []
        errors.extend(
            f"Unerlaubtes Feld: {k}"
            for k in sorted(set(response) - required - {"completion_checks"})
        )
        if errors:
            return response, errors

        if response["decision"] not in DECISIONS:
            errors.append("Entscheidung ist nicht erlaubt")
        if response["machine_state"] not in MACHINE_STATES:
            errors.append("Maschinenzustand ist nicht erlaubt")
        if type(response["read_only"]) is not bool:
            errors.append("read_only muss bool sein")
        if type(response["wait"]) is not bool:
            errors.append("wait muss bool sein")
        if type(response["safe_state_required"]) is not bool:
            errors.append("safe_state_required muss bool sein")
        if (
            type(response["confidence"]) not in {int, float}
            or isinstance(response["confidence"], bool)
        ):
            errors.append("Konfidenz ist ungueltig")
        elif not 0 <= response["confidence"] <= 1:
            errors.append("Konfidenz muss zwischen 0 und 1 liegen")
        if not isinstance(response["observations"], list) or not all(
            isinstance(x, str) for x in response["observations"]
        ):
            errors.append("observations muss eine Liste von Texten sein")
        if not isinstance(response["anomalies"], list) or not all(
            isinstance(x, str) for x in response["anomalies"]
        ):
            errors.append("anomalies muss eine Liste von Texten sein")
        if not isinstance(response["summary"], str) or not response["summary"].strip():
            errors.append("summary darf nicht leer sein")

        checks = response["completion_checks"]
        if not isinstance(checks, list) or len(checks) > 20:
            errors.append("completion_checks muss eine Liste mit max. 20 Eintraegen sein")
        elif checks and not all(
            isinstance(c, dict)
            and set(c) == {"symbol", "value"}
            and isinstance(c["symbol"], str)
            and c["symbol"]
            for c in checks
        ):
            errors.append("completion_checks muss exakt symbol und value enthalten")

        if (
            type(response["wait_seconds"]) not in {int, float}
            or isinstance(response["wait_seconds"], bool)
            or response["wait_seconds"] < 0
        ):
            errors.append("wait_seconds ist ungueltig oder negativ")

        actions = response["requested_actions"]
        if not isinstance(actions, list):
            errors.append("requested_actions muss eine Liste sein")
            actions = []
            response["requested_actions"] = []
        elif len(actions) > 1:
            errors.append("Pro Entscheidung ist maximal eine Aktion erlaubt")

        # Sichere Normalisierung widerspruechlicher Warte-/Aktionsantworten.
        # wait hat Vorrang: niemals schreiben, wenn die Antwort wait=true oder
        # wait_seconds>0 enthaelt. Das verhindert genau den Gemma-Fehler
        # decision=continue + Aktion + wait=true.
        has_wait_intent = (
            response["wait"] is True or response["wait_seconds"] > 0
        )
        if has_wait_intent and response["decision"] != "fault":
            if response["decision"] != "wait":
                self._last_response_warnings.append(
                    "Widerspruch normalisiert: wait hat Vorrang; "
                    "Schreibaktion wurde verworfen und decision=wait gesetzt."
                )
                response["decision"] = "wait"
            if response["requested_actions"]:
                self._last_response_warnings.append(
                    "Schreibaktion trotz Warteanforderung verworfen."
                )
            response["requested_actions"] = []
            response["read_only"] = True
            response["wait"] = True
        elif response["decision"] == "wait" and response["requested_actions"]:
            self._last_response_warnings.append(
                "Schreibaktion bei decision=wait verworfen."
            )
            response["requested_actions"] = []
            response["read_only"] = True

        # Abschlusspruefungen sind nur fuer completed sinnvoll.
        if response["decision"] != "completed" and response["completion_checks"]:
            self._last_response_warnings.append(
                "completion_checks ausserhalb von completed verworfen."
            )
            response["completion_checks"] = []

        # read_only wird deterministisch an die tatsaechliche Aktionsliste
        # angepasst. Das verhindert widerspruechliche Modellfelder.
        if response["requested_actions"]:
            response["read_only"] = False
        else:
            response["read_only"] = True

        # Nach Normalisierung die verbindlichen Beziehungen pruefen.
        decision = response["decision"]
        actions = response["requested_actions"]
        if decision == "continue" and len(actions) != 1:
            errors.append("continue muss genau eine Aktion enthalten")
        if decision != "continue" and actions:
            errors.append("Nur continue darf eine Schreibaktion enthalten")
        if decision == "completed" and not response["completion_checks"]:
            errors.append("completed erfordert mindestens eine SPS-Abschlusspruefung")
        if decision == "wait" and response["wait_seconds"] <= 0:
            errors.append("wait muss eine positive Wartezeit enthalten")
        if decision != "wait" and response["wait_seconds"] != 0:
            errors.append("wait_seconds darf nur bei wait gesetzt sein")
        if decision == "wait" and response["wait"] is not True:
            errors.append("wait muss bei decision=wait true sein")
        if decision != "wait" and response["wait"] is not False:
            errors.append("wait muss ausserhalb von wait false sein")

        if actions:
            a = actions[0]
            if not isinstance(a, dict) or set(a) != {"symbol", "value", "reason"}:
                errors.append("Aktion muss exakt symbol, value und reason enthalten")
            elif not isinstance(a["symbol"], str) or not a["symbol"]:
                errors.append("Aktionssymbol ist ungueltig")
            elif not isinstance(a["reason"], str) or not a["reason"].strip():
                errors.append("Aktionsbegruendung darf nicht leer sein")

        return response, errors

    # ── Zeitstempel- und Konfidenzpruefung ────────────────────────────────────

    def _validate_timestamp_and_confidence(
        self, response: dict[str, Any], limits: dict[str, Any]
    ) -> list[str]:
        errors: list[str] = []
        try:
            stamp = datetime.fromisoformat(
                str(response["timestamp"]).replace("Z", "+00:00")
            )
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                errors.append("Zeitstempel besitzt keine Zeitzone")
            else:
                age = (
                    datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
                ).total_seconds()
                if age < -5:
                    errors.append("Zeitstempel liegt unzulaessig in der Zukunft")
                elif age > float(self.machine.get("max_response_age_seconds", 15)):
                    errors.append("LLM-Antwort ist veraltet")
        except (TypeError, ValueError):
            errors.append("Zeitstempel ist ungueltig")
        if response["confidence"] < float(self.machine.get("min_confidence", 0.85)):
            errors.append("Konfidenz ist zu niedrig")
        if response["wait_seconds"] > float(limits["max_wait_seconds"]):
            errors.append("Wartezeit ueberschreitet das konfigurierte Limit")
        return errors

    # ── Ausfuehrungsbedingungen ───────────────────────────────────────────────

    def _execution_conditions(
        self, snapshot: dict[str, dict[str, Any]]
    ) -> list[str]:
        errors: list[str] = []
        execution = self.machine.get("execution", {})
        for symbol in execution.get("required_true", []):
            if snapshot.get(symbol, {}).get("value") is not True:
                errors.append(f"Freigabe fehlt: {symbol}")
        for symbol in execution.get("required_false", []):
            if snapshot.get(symbol, {}).get("value") is not False:
                errors.append(f"Verriegelung aktiv oder ungueltig: {symbol}")
        mode_symbol = execution.get("mode_symbol")
        allowed_modes = execution.get("allowed_modes", [])
        if mode_symbol and allowed_modes:
            mode_values = execution.get("mode_values", {})
            mode_name = mode_values.get(
                str(snapshot.get(mode_symbol, {}).get("value"))
            )
            if mode_name not in allowed_modes:
                errors.append(f"Betriebsart nicht freigegeben: {mode_name}")
        return errors

    # ── Typueberpruefung ──────────────────────────────────────────────────────

    def _type_ok(self, spec: dict[str, Any], value: Any) -> bool:
        dt = spec.get("data_type")
        if dt == "BOOL":
            return type(value) is bool
        if dt in {"INT", "DINT", "UINT", "UDINT", "TIME"}:
            return type(value) is int
        if dt in {"REAL", "LREAL"}:
            return type(value) in {int, float} and not isinstance(value, bool)
        if dt == "STRING":
            return type(value) is str
        return False

    # ── Schreibvalidierung ────────────────────────────────────────────────────

    def _validate_action(
        self,
        response: dict[str, Any],
        before: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        write_enabled: bool | Callable[[], bool],
        limits: dict[str, Any],
        session: ProcessSession,
    ) -> list[str]:
        # Stopp-Flag als erste Pruefung
        if session.is_stop_requested():
            return ["Stopp wurde angefordert. Kein Schreibvorgang."]

        errors = self._validate_timestamp_and_confidence(response, limits)
        current_write_enabled = bool(
            write_enabled() if callable(write_enabled) else write_enabled
        )
        if self.machine.get("enabled") is not True:
            errors.append("Maschinenbeschreibung ist deaktiviert")

        action = response["requested_actions"][0]
        symbol = action["symbol"]
        spec = self._spec(symbol)
        if spec is None:
            errors.append(f"Symbol nicht in Maschinenbeschreibung: {symbol}")
            return errors

        if not current_write_enabled:
            errors.append("Schreibmodus ist in der Weboberflaeche nicht freigegeben")
        if spec.get("writable") is not True or spec.get("role") not in {"actuator", None}:
            errors.append(f"Symbol nicht als schreibbarer Aktor freigegeben: {symbol}")
        if not current.get(symbol, {}).get("valid"):
            errors.append(f"Aktionssymbol ist im aktuellen Snapshot ungueltig: {symbol}")
        if not self._type_ok(spec, action["value"]):
            errors.append(f"Datentyp passt nicht: {symbol}")
        allowed = spec.get("allowed_values")
        if allowed and not any(_same_value(action["value"], x) for x in allowed):
            errors.append(f"Wert nicht erlaubt: {symbol}")

        errors.extend(self._execution_conditions(current))

        # Zustandsaenderung seit letztem Snapshot?
        for sym, state in before.items():
            new_state = current.get(sym, {})
            if not new_state.get("valid") or not _same_value(
                state.get("value"), new_state.get("value")
            ):
                errors.append(f"Anlagenzustand hat sich unerwartet geaendert: {sym}")

        self._prune_writes()
        if len(self.write_times) + 1 > int(limits["max_writes_per_minute"]):
            errors.append("Maximale Schreibfrequenz ueberschritten")

        return errors

    def _prune_writes(self) -> None:
        cutoff = time.monotonic() - 60.0
        self.write_times = [t for t in self.write_times if t >= cutoff]

    # ── Feedback-Pruefung ─────────────────────────────────────────────────────

    def _feedback(self, spec: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for expected in spec.get("expected_feedback", []) or []:
            target = self._spec(expected.get("symbol"))
            if target is None:
                errors.append(
                    f"Feedbacksymbol nicht beschrieben: {expected.get('symbol')}"
                )
                continue
            timeout = float(expected.get("timeout_seconds", 5.0))
            interval = float(expected.get("poll_interval_seconds", 0.1))
            deadline = time.monotonic() + timeout
            reached = False
            last_error = ""
            while time.monotonic() <= deadline:
                value, ok, error = self.ads.read_value(
                    target["symbol"], target["data_type"]
                )
                last_error = error
                if ok and _same_value(value, expected.get("value")):
                    reached = True
                    break
                self.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            if not reached:
                suffix = f": {last_error}" if last_error else ""
                errors.append(
                    f"Feedback nicht erreicht: "
                    f"{expected.get('symbol')}={expected.get('value')}{suffix}"
                )
        return errors

    # ── Wiederholungserkennung ────────────────────────────────────────────────

    def _fingerprint(self, response: dict[str, Any]) -> str:
        return json.dumps(
            {
                "decision":    response["decision"],
                "actions":     response["requested_actions"],
                "wait":        response["wait"],
                "wait_seconds": response["wait_seconds"],
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    # ── Fehlerquittierung ─────────────────────────────────────────────────────

    def _handle_fault_and_wait_ack(
        self, session: ProcessSession, limits: dict[str, Any]
    ) -> WakeResult:
        """Wartet auf das Quittierungssignal (role='fault_ack').

        Die Anwendung schreibt das Signal NICHT selbst.
        Gibt "woken" (quittiert), "stopped" oder "timeout" zurueck.
        """
        ack_spec = self._find_role("fault_ack")
        if ack_spec is None:
            self._emit({
                "event":   "fault_no_ack_signal",
                "job_id":  self.job_id,
                "message": "Kein Symbol mit role='fault_ack' konfiguriert. "
                           "Auftrag wird abgebrochen.",
            })
            return "timeout"

        timeout = float(limits["ack_timeout_seconds"])
        session.status = ProcessStatus.WAITING_ACK
        session.next_wakeup_reason = "ack"
        session.wakeup_condition = {
            "symbol": ack_spec["symbol"],
            "value":  True,
        }
        session.waiting_until = _iso_deadline(timeout)

        self._emit({
            "event":      "waiting_for_ack",
            "job_id":     self.job_id,
            "ack_symbol": ack_spec["symbol"],
            "message":    f"Warte auf Fehlerquittierung: {ack_spec['symbol']}",
        })

        result = wait_for_ack(
            ads=self.ads,
            ack_symbol=ack_spec["symbol"],
            data_type=ack_spec["data_type"],
            timeout=timeout,
            poll_interval=float(limits["ack_poll_interval"]),
            stop_event=session.stop_requested,
            sleep_fn=self.sleep,
        )

        session.waiting_until = None
        session.next_wakeup_reason = None
        session.wakeup_condition = None
        return result

    # ── Innerer Zyklus (eine vollstaendige Entscheidungsschleife) ─────────────

    def _run_cycle(
        self,
        session: ProcessSession,
        command: str,
        write_enabled: bool | Callable[[], bool],
        limits: dict[str, Any],
        cycle_start: float,
    ) -> dict[str, Any]:
        """Fuehrt einen Zyklus (max_steps Entscheidungen) aus.

        Gibt ein Dict mit "status" und "message" zurueck.
        Schreibt Schritte direkt in session.steps.
        """
        steps: list[dict[str, Any]] = []
        writes_this_cycle = 0
        repeated: dict[str, int] = {}

        snapshot, errors = self._snapshot()
        if errors:
            return {
                "status":  "failed",
                "message": "Snapshot unvollstaendig. Keine Aktion ausgefuehrt.",
                "errors":  errors,
            }

        for step_index in range(1, int(limits["max_steps"]) + 1):
            session.step_count = step_index
            session.total_step_count += 1

            # Gesamtzeitlimit: der kleinere positive Wert aus job- und cycle-timeout gilt.
            job_timeout = float(limits.get("job_timeout_seconds", 0) or 0)
            cycle_timeout = float(limits.get("cycle_timeout_seconds", 0) or 0)
            effective_timeout = min(
                value for value in (job_timeout, cycle_timeout) if value > 0
            ) if any(value > 0 for value in (job_timeout, cycle_timeout)) else 0
            if effective_timeout and time.monotonic() - cycle_start > effective_timeout:
                return {
                    "status":  "aborted",
                    "message": "Gesamtzeitlimit ueberschritten.",
                }

            # Stopp-Flag
            if session.is_stop_requested():
                return {
                    "status":  "aborted",
                    "message": "Auftrag durch Bediener gestoppt.",
                }

            # LLM anfragen
            raw, llm_ok = self._ask(
                self._prompt(command, snapshot, session.cycle_count)
            )
            step: dict[str, Any] = {
                "step":           step_index,
                "cycle":          session.cycle_count,
                "started_at":     _now(),
                "snapshot_before": snapshot,
                "llm_raw":        raw if isinstance(raw, str) else repr(raw),
            }

            if not llm_ok:
                step["status"] = "failed"
                step["errors"] = [str(raw)]
                steps.append(step)
                session.steps.extend(steps)
                return {
                    "status":  "failed",
                    "message": "Lokales LLM nicht verfuegbar. Keine Aktion ausgefuehrt.",
                }

            response, parse_errors = self._parse_response(raw)
            if response is None or parse_errors:
                step["status"] = "failed"
                step["errors"] = parse_errors
                step["response"] = response
                steps.append(step)
                session.steps.extend(steps)
                return {
                    "status":  "failed",
                    "message": "LLM-Antwort wurde strikt abgelehnt. Keine Aktion ausgefuehrt.",
                    "errors":  parse_errors,
                }

            step["response"] = response
            if self._last_response_warnings:
                step["normalization_warnings"] = list(self._last_response_warnings)
            session.last_step_summary = response.get("summary", "")

            self._emit({
                "event":    "decision_received",
                "job_id":   self.job_id,
                "step":     step_index,
                "cycle":    session.cycle_count,
                "decision": response,
            })

            # Wiederholungserkennung
            fp = self._fingerprint(response)
            repeated[fp] = repeated.get(fp, 0) + 1
            if repeated[fp] > int(limits["max_identical_decisions"]):
                step["status"] = "failed"
                step["errors"] = ["Wiederholte identische KI-Entscheidung erkannt"]
                steps.append(step)
                session.steps.extend(steps)
                return {
                    "status":  "failed",
                    "message": "Ablauf wegen wiederholter identischer Entscheidung abgebrochen.",
                }

            decision = response["decision"]

            # ── completed ──────────────────────────────────────────────────
            if decision == "completed":
                if response["machine_state"] != "erreicht":
                    step["status"] = "failed"
                    step["errors"] = ["completed erfordert machine_state=erreicht"]
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Abschluss nicht durch Maschinenzustand bestaetigt.",
                    }
                final_snap, final_errors = self._snapshot()
                completion_errors = list(final_errors)
                for check in response["completion_checks"]:
                    state = final_snap.get(check["symbol"])
                    if state is None:
                        completion_errors.append(
                            f"Abschlusspruefung unbekanntes Symbol: {check['symbol']}"
                        )
                    elif not state.get("valid") or not _same_value(
                        state.get("value"), check["value"]
                    ):
                        completion_errors.append(
                            f"Abschlusspruefung nicht erfuellt: "
                            f"{check['symbol']}={check['value']}"
                        )
                if completion_errors:
                    step["status"] = "failed"
                    step["errors"] = completion_errors
                    step["snapshot_completion"] = final_snap
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "SPS-Zustand bestaetigt das Ziel nicht.",
                    }
                step["status"] = "completed"
                step["snapshot_completion"] = final_snap
                steps.append(step)
                session.steps.extend(steps)
                return {
                    "status":  "completed",
                    "message": response["summary"],
                }

            # ── fault ──────────────────────────────────────────────────────
            if decision == "fault":
                step["status"] = "fault"
                step["errors"] = response["anomalies"] or [response["summary"]]
                steps.append(step)
                session.steps.extend(steps)
                return {
                    "status":  "fault",
                    "message": response["summary"],
                }

            # ── blocked / unclear ──────────────────────────────────────────
            if decision in {"blocked", "unclear"}:
                step["status"] = decision
                step["errors"] = response["anomalies"] or [response["summary"]]
                steps.append(step)
                session.steps.extend(steps)
                return {
                    "status":  decision,
                    "message": response["summary"],
                }

            # ── wait ───────────────────────────────────────────────────────
            if decision == "wait":
                if response["safe_state_required"]:
                    step["status"] = "safe_state"
                    step["errors"] = ["LLM fordert sicheren Zustand an"]
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Sicherer Zustand erforderlich. Keine weitere Aktion.",
                    }

                wait_secs = float(response["wait_seconds"])
                session.status = ProcessStatus.WAITING_TIMER
                session.waiting_until = _iso_deadline(wait_secs)
                session.next_wakeup_reason = "timer"

                step["status"] = "waiting"
                step["wait_seconds"] = wait_secs
                steps.append(step)

                self._emit({
                    "event":   "wait",
                    "job_id":  self.job_id,
                    "step":    step_index,
                    "cycle":   session.cycle_count,
                    "seconds": wait_secs,
                })

                wake = wait_for_timer(
                    seconds=wait_secs,
                    stop_event=session.stop_requested,
                    sleep_fn=self.sleep,
                )

                session.waiting_until = None
                session.next_wakeup_reason = None
                session.status = ProcessStatus.RUNNING

                if wake == "stopped":
                    session.steps.extend(steps)
                    return {
                        "status":  "aborted",
                        "message": "Stopp waehrend Wartezustand angefordert.",
                    }

                snapshot, snap_errors = self._snapshot()
                if snap_errors:
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Snapshot nach Wartezustand unvollstaendig.",
                        "errors":  snap_errors,
                    }
                continue

            # ── continue ───────────────────────────────────────────────────
            if decision == "continue":
                # Snapshot direkt vor Schreiben
                session.status = ProcessStatus.EXECUTING
                current, read_errors = self._snapshot()
                if read_errors:
                    step["status"] = "failed"
                    step["errors"] = read_errors
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Snapshot vor Schreibvorgang unvollstaendig.",
                    }

                # Schreibanzahl pro Auftrag
                if session.total_write_count + 1 > int(limits["max_writes_per_job"]):
                    step["status"] = "failed"
                    step["errors"] = ["Maximale Schreibanzahl pro Auftrag ueberschritten"]
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Maximale Schreibanzahl pro Auftrag ueberschritten.",
                    }

                validation_errors = self._validate_action(
                    response, snapshot, current, write_enabled, limits, session
                )
                if validation_errors:
                    step["status"] = "failed"
                    step["errors"] = validation_errors
                    step["snapshot_at_validation"] = current
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Pruefung nicht bestanden. Keine Aktion ausgefuehrt.",
                    }

                action = response["requested_actions"][0]
                spec = self._spec(action["symbol"])
                ok, error = self.ads.write_value(
                    action["symbol"], spec["data_type"], action["value"]
                )
                self.write_times.append(time.monotonic())
                session.total_write_count += 1
                writes_this_cycle += 1

                write_result: dict[str, Any] = {
                    "symbol":    action["symbol"],
                    "requested": _json_safe(action["value"]),
                    "write_ok":  ok,
                    "error":     error,
                }
                step["write"] = write_result

                if not ok:
                    step["status"] = "failed"
                    step["errors"] = ["ADS-Schreibfehler"]
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "ADS-Schreibfehler. Keine weitere Aktion.",
                    }

                actual, read_ok, read_error = self.ads.read_value(
                    action["symbol"], spec["data_type"]
                )
                write_result.update({
                    "actual":          _json_safe(actual),
                    "readback_ok":     read_ok,
                    "readback_error":  read_error,
                })
                if not read_ok or not _same_value(actual, action["value"]):
                    step["status"] = "failed"
                    step["errors"] = ["Ruecklesen weicht vom Schreibwert ab"]
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Ruecklesen weicht vom Schreibwert ab.",
                    }

                feedback_errors = self._feedback(spec)
                write_result["feedback_errors"] = feedback_errors
                if feedback_errors:
                    step["status"] = "failed"
                    step["errors"] = feedback_errors
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Erwartete Sensorreaktion blieb aus.",
                    }

                snapshot, snap_errors = self._snapshot()
                step["snapshot_after"] = snapshot
                step["status"] = "executed"
                session.status = ProcessStatus.RUNNING

                if snap_errors:
                    step["status"] = "failed"
                    step["errors"] = snap_errors
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Snapshot nach Schreibvorgang unvollstaendig.",
                    }

                steps.append(step)
                self._emit({
                    "event":          "step_executed",
                    "job_id":         self.job_id,
                    "step":           step_index,
                    "cycle":          session.cycle_count,
                    "decision":       response,
                    "write":          write_result,
                    "snapshot_after": snapshot,
                })
                self._emit({
                    "event":   "next_decision_pending",
                    "job_id":  self.job_id,
                    "step":    step_index,
                    "message": "Schritt bestaetigt. Naechster realer SPS-Snapshot wird bewertet.",
                })
                continue

        # max_steps erschoepft
        session.steps.extend(steps)
        return {
            "status":  "step_limit",
            "message": "Maximale Anzahl von Entscheidungsschritten ueberschritten.",
        }

    # ── Supervisor-Schleife (oeffentliche Methode) ────────────────────────────

    def execute(
        self,
        command: str,
        write_enabled: bool | Callable[[], bool],
        job_id: str | None = None,
        session: ProcessSession | None = None,
    ) -> dict[str, Any]:
        """Fuehrt den Benutzerauftrag aus.

        Bei loop_mode=false: ein Zyklus (Verhalten identisch zu vorher).
        Bei loop_mode=true:  Dauerschleife bis Stopp, Fehler oder Limit.
        """
        command = str(command or "").strip()
        if not command:
            return {
                "ok": False, "status": "failed",
                "message": "Kein Benutzerbefehl eingegeben.",
            }
        if not getattr(self.ads, "connected", False):
            return {
                "ok": False, "status": "failed",
                "message": "ADS ist nicht verbunden. Keine Aktion ausgefuehrt.",
            }

        self.job_id = job_id or uuid.uuid4().hex
        limits = self._limits()

        # Sitzung anlegen oder uebergeben (fuer Tests)
        if session is None:
            session = ProcessSession(
                process_id=self.job_id,
                command=command,
            )
        self.session = session
        session.status = ProcessStatus.RUNNING
        session.started_at = _now()

        overall_start = time.monotonic()
        loop_mode = bool(limits["loop_mode"])
        max_cycles = int(limits["max_cycles"])

        while True:
            # Stopp-Flag
            if session.is_stop_requested():
                return self._finish_session(
                    session, False, "aborted",
                    "Auftrag durch Bediener gestoppt.", overall_start
                )

            # Gesamtzeitlimit
            if time.monotonic() - overall_start > float(limits["cycle_timeout_seconds"]):
                return self._finish_session(
                    session, False, "aborted",
                    "Gesamtzeitlimit ueberschritten.", overall_start
                )

            # Zyklus-Limit (0 = unbegrenzt)
            if max_cycles > 0 and session.cycle_count >= max_cycles:
                return self._finish_session(
                    session, False, "aborted",
                    f"Maximale Zyklusanzahl ({max_cycles}) erreicht.", overall_start
                )

            # Einen Zyklus ausfuehren
            cycle_result = self._run_cycle(
                session, command, write_enabled, limits, overall_start
            )
            session.cycle_count += 1

            status = cycle_result["status"]

            if status == "completed":
                if loop_mode:
                    # Dauerschleife: neuer Zyklus
                    session.status = ProcessStatus.RUNNING
                    session.step_count = 0
                    self._emit({
                        "event":   "cycle_completed",
                        "job_id":  self.job_id,
                        "cycle":   session.cycle_count,
                        "message": "Zyklus abgeschlossen. Neuer Zyklus startet.",
                    })
                    continue
                else:
                    return self._finish_session(
                        session, True, "completed",
                        cycle_result["message"], overall_start,
                        cycle_result.get("errors"),
                    )

            if status == "fault":
                session.status = ProcessStatus.FAULT
                self._emit({
                    "event":   "fault_detected",
                    "job_id":  self.job_id,
                    "cycle":   session.cycle_count,
                    "message": cycle_result["message"],
                })
                ack_result = self._handle_fault_and_wait_ack(session, limits)

                if ack_result == "stopped":
                    return self._finish_session(
                        session, False, "aborted",
                        "Stopp waehrend Fehlerquittierung angefordert.", overall_start
                    )
                if ack_result == "timeout":
                    return self._finish_session(
                        session, False, "failed",
                        "Fehlerquittierung nicht innerhalb des Zeitlimits erhalten.",
                        overall_start
                    )

                # Quittierung erhalten: neuer Zyklus (auch ohne loop_mode)
                session.status = ProcessStatus.RUNNING
                session.step_count = 0
                self._emit({
                    "event":   "fault_acknowledged",
                    "job_id":  self.job_id,
                    "cycle":   session.cycle_count,
                    "message": "Fehler quittiert. Neuer Zyklus startet.",
                })
                continue

            # Alle anderen Endzustaende
            ok = status in {"completed"}
            return self._finish_session(
                session, ok, status, cycle_result["message"], overall_start,
                cycle_result.get("errors"),
            )

    # ── Abschluss ─────────────────────────────────────────────────────────────

    def _finish_session(
        self,
        session: ProcessSession,
        ok: bool,
        status: str,
        message: str,
        overall_start: float,
        errors: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            session.status = ProcessStatus(status)
        except ValueError:
            session.status = ProcessStatus.FAILED
        session.finished_at = _now()
        elapsed = round(time.monotonic() - overall_start, 3)
        return {
            "ok":               ok,
            "status":           status,
            "job_id":           self.job_id,
            "message":          message,
            "cycle_count":      session.cycle_count,
            "step_count":       session.total_step_count,
            "write_count":      session.total_write_count,
            "elapsed_seconds":  elapsed,
            "steps":            session.steps,
            "session":          session.public(),
            "errors":            errors or [],
        }
