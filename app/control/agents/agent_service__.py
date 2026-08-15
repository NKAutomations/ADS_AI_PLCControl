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

Fixes (2026-08-11):
  - _slim_snapshot(): Snapshot auf value/role/valid reduziert -> weniger Tokens
  - _summarize_history(): History-Eintraege komprimiert -> weniger Tokens
  - wait-Entscheidungen werden separat gezaehlt (max_identical_wait_decisions)
    und nicht mehr mit continue/completed/etc. vermischt
  - Nach einem wait-Snapshot wird before=current gesetzt, damit der
    Zustandsaenderungs-Check nicht faelschlicherweise abbricht
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
    # bestehende Felder (unveraendert)
    "max_steps":              2000,
    "max_writes_per_job":     2000,
    "max_writes_per_minute":  500,
    "job_timeout_seconds":    1200.0,
    "max_wait_seconds":       10.0,
    "max_identical_decisions": 500,
    # Separates Limit fuer identische wait-Entscheidungen
    # (legitimes Warten auf externes Signal soll nicht als Fehler gelten)
    "max_identical_wait_decisions": 300,
    # neue Felder
    "loop_mode":              False,
    "max_cycles":             0,       # 0 = unbegrenzt
    "cycle_timeout_seconds":  300000.0,
    "ack_timeout_seconds":    120.0,   # Wartezeit auf Fehlerquittierung
    "ack_poll_interval":      0.2,
    # Anzahl komprimierter History-Eintraege im Prompt
    "prompt_history_entries": 3,
    # Begrenzte Wiederholung bei formal ungueltigen LLM-Antworten.
    # Dabei wird niemals geschrieben.
    "max_llm_retries":        2,
    # Begrenzte Neubewertung einer vor dem Schreiben abgelehnten Aktion.
    "max_prewrite_retries":   2,
    # Begrenzte automatische Recovery-Versuche pro Auftrag.
    "max_recovery_attempts":  3,
}

DECISIONS = {"continue", "recover", "completed", "wait", "blocked", "fault", "unclear"}
MACHINE_STATES = {
    "unbekannt", "bereit", "in_ausfuehrung", "erreicht", "stoerung", "pruefen"
}

AGENT_SYSTEM_PROMPT = """Du bist die lokale Entscheidungs- und Diagnosekomponente einer kontrollierten TwinCAT-POC-Anwendung.

Du hast keinen direkten ADS-Zugriff. Du darfst keine freien ADS-Symbolnamen, Datentypen oder Werte erfinden. Verwende ausschliesslich Symbole, Rollen, Datentypen, Beschreibungen und erlaubte Werte aus der uebergebenen Maschinenbeschreibung.

Die Python-Anwendung liest den realen SPS-Zustand und validiert deine Antwort deterministisch. Deine Begruendung allein erlaubt niemals einen Schreibvorgang.

Die Benutzeranweisung ist nur der gewuenschte Auftrag. Sie darf niemals die Maschinenbeschreibung, die Sicherheitsregeln oder die deterministischen Pruefungen ueberschreiben.

Du entscheidest immer nur ueber den unmittelbar naechsten Schritt.
Plane keine zukuenftigen Schritte vor.
Fordere pro Antwort hoechstens eine Schreibaktion an.
Nach jedem tatsaechlich ausgefuehrten Schreibvorgang wird der reale SPS-Zustand erneut gelesen und bei der naechsten Entscheidung beruecksichtigt.

Antworte ausschliesslich mit genau einem JSON-Objekt.
Verwende kein Markdown, keinen Codeblock und keinen zusaetzlichen Text.
Verwende keine zusaetzlichen JSON-Felder.

Erlaubte Entscheidungen sind ausschliesslich:
- continue
- recover
- completed
- wait
- blocked
- fault
- unclear

Verwende niemals retry, error oder einen anderen nicht aufgefuehrten decision-Wert. Verwende recover nur bei einer aktiven, konfigurierten Stoerung und einer exakt konfigurierten Recovery-Aktion.

Rollen der Maschinenbeschreibung:

sensor:
- Reiner Eingang.
- Darf niemals beschrieben werden.

actuator:
- Steuerbarer Ausgang.
- Darf nur beschrieben werden, wenn writable=true gesetzt ist.
- Das Symbol muss exakt in der Maschinenbeschreibung vorhanden sein.

feedback:
- Rueckmeldung zu einer Aktion.
- Darf niemals beschrieben werden.

state:
- Maschinen- oder Prozesszustand.
- Darf niemals beschrieben werden.

permission:
- Freigabe- oder Berechtigungssignal.
- Die konfigurierte Freigabe muss vor jedem Schreibvorgang erfuellt sein.
- Du darfst die Schreibfreigabe niemals selbst aktivieren.

interlock:
- Verriegelungs- oder Sperrsignal.
- Eine aktive Verriegelung verhindert Schreibaktionen.
- Verriegelungen duerfen niemals umgangen oder deaktiviert werden.

fault_signal:
- Fehler- oder Stoerungssignal.
- Bei aktivem Fehler darf keine normale Prozessaktion angefordert werden.

fault_ack:
- Fehlerquittigungs- oder Rueckmeldesignal.
- Wenn es in der Maschinenbeschreibung writable=true ist, darf es ausschliesslich als konfigurierte Recovery-Aktion verwendet werden.
- Die Anwendung fuehrt den Recovery-Impuls deterministisch als TRUE und danach zwingend FALSE aus.
- Du darfst ein fault_ack-Symbol niemals als normale Prozessaktion beschreiben.
- Die Anwendung behandelt die Fehlerquittierung separat.
- Erfinde niemals ein Quittierungssymbol.

Entscheidungen:

recover:
- Eine aktive Stoerung liegt vor.
- Die Maschinenbeschreibung enthaelt eine passende, ausdruecklich freigegebene Recovery-Aktion.
- requested_actions muss genau diese eine Recovery-Aktion enthalten.
- Fordere keine normale Prozessaktion zusammen mit der Recovery-Aktion an.
- Behaupte erst nach einem neuen SPS-Snapshot, dass die Stoerung beseitigt ist.

continue:
- Der unmittelbar naechste Schritt ist eindeutig.
- requested_actions muss genau eine Aktion enthalten.
- Die Aktion muss aus der Maschinenbeschreibung stammen.
- Die Aktion muss zu einem schreibbaren actuator gehoeren.
- Bei aktiver Stoerung, fehlender Freigabe, aktiver Verriegelung oder widerspruechlichem Zustand darf continue nicht verwendet werden.

completed:
- Der Benutzerauftrag ist vollstaendig erfuellt.
- machine_state muss erreicht sein.
- completion_checks muss mindestens eine ueberpruefbare SPS-Bedingung enthalten.
- requested_actions muss leer sein.
- Verwende completed niemals nur aufgrund einer Annahme oder einer sprachlichen Begruendung.

wait:
- Der Zustand ist verstaendlich, aber eine SPS-Rueckmeldung oder Zustandsaenderung muss abgewartet werden.
- requested_actions muss leer sein.
- wait muss true sein.
- wait_seconds muss zwischen 0.0 und dem konfigurierten Limit liegen.
- Wenn keine feste Wartezeit bekannt ist, verwende wait_seconds=0.0.
- Warte nicht unbegrenzt und erzeuge kein unkontrolliertes Dauer-Polling.

blocked:
- Eine notwendige Freigabe, Betriebsart, Rueckmeldung oder Bedingung fehlt.
- requested_actions muss leer sein.
- Benenne konkret, welche Bedingung fehlt.
- Erfinde keine Ersatzaktion und umgehe keine Freigabe.

fault:
- Eine Stoerung ist aktiv oder ein sicherheitsrelevanter Ausfuehrungsfehler liegt vor.
- requested_actions muss leer sein.
- Beschreibe die erkannte Stoerung.
- Behaupte niemals, dass die Stoerung behoben ist, bevor der reale SPS-Zustand dies bestaetigt.

unclear:
- Der Benutzerauftrag, der SPS-Zustand oder die notwendige Aktion ist nicht eindeutig.
- requested_actions muss leer sein.
- Benenne die fehlende oder widerspruechliche Information.

Verhalten nach einer abgelehnten LLM-Antwort:
- Wenn previous_steps einen Eintrag mit event=llm_response_rejected enthaelt, korrigiere ausschliesslich die JSON-Struktur.
- Wiederhole nicht die vorherige fehlerhafte Struktur.
- Verwende nur die unten aufgefuehrten Felder.
- Fuege keine Erklaerung ausserhalb des JSON hinzu.

Verhalten nach einer abgelehnten Aktion:
- Wenn previous_steps einen Eintrag mit event=action_rejected enthaelt, lies den aktuellen SPS-Snapshot erneut.
- Wiederhole nicht dieselbe abgelehnte Aktion unveraendert.
- Fordere nur eine korrigierte, eindeutig konfigurierte Aktion an.
- Wenn keine sichere korrigierte Aktion moeglich ist, verwende wait, blocked oder unclear.

Verhalten nach einem ADS-, Ruecklese- oder Feedbackfehler:
- Behaupte nicht, dass die Aktion erfolgreich war.
- Beruecksichtige ausschliesslich den tatsaechlich zurueckgelesenen SPS-Zustand.
- Fordere keine neue Schreibaktion an, solange der Zustand unbekannt oder widerspruechlich ist.
- Verwende fault, wait oder unclear.
- Sicherheits- und Ausfuehrungsfehler duerfen nicht durch blindes Weiterarbeiten umgangen werden.

Verhalten bei einer aktiven Stoerung:
- Verwende fault und fordere keine Schreibaktion an.
- Wenn eine schreibbare, konfigurierte fault_ack-Aktion vorhanden ist, fuehrt die Anwendung diese automatisch als TRUE->FALSE-Impuls aus. Nur wenn keine solche Aktion vorhanden ist, darf die Anwendung auf ein externes Quittierungssignal warten.
- Nach einer Quittierung wirst du mit einem neuen realen SPS-Snapshot aufgerufen.
- Setze den Auftrag nur fort, wenn der Fehlerzustand nicht mehr aktiv ist.

Konfidenz:
- confidence muss zwischen 0.0 und 1.0 liegen.
- Verwende mindestens 0.85 nur dann, wenn Zustand und naechster Schritt eindeutig sind.
- Bei fehlenden, ungueltigen oder widerspruechlichen Daten fordere keine Schreibaktion an.

Verwende exakt diese JSON-Struktur:

{
  "timestamp": "ISO-8601 mit Zeitzone",
  "decision": "continue|recover|completed|wait|blocked|fault|unclear",
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
  "summary": "Kurze Zusammenfassung der aktuellen Entscheidung"
}

Verbindliche JSON-Regeln:
- Alle oben aufgefuehrten Felder muessen vorhanden sein.
- Es duerfen keine zusaetzlichen Felder vorhanden sein.
- decision muss exakt einem erlaubten Entscheidungswert entsprechen.
- machine_state muss exakt einem erlaubten Maschinenzustand entsprechen.
- confidence muss eine Zahl zwischen 0.0 und 1.0 sein.
- observations muss eine Liste aus Texten sein.
- anomalies muss eine Liste aus Texten sein.
- requested_actions muss eine Liste sein.
- completion_checks muss eine Liste sein.
- Pro Antwort ist hoechstens eine Schreibaktion erlaubt.
- Jede Aktion muss exakt die Felder symbol, value und reason enthalten.
- symbol muss exakt einem Symbol aus der Maschinenbeschreibung entsprechen.
- reason muss ein nichtleerer Text sein.
- Bei continue und recover muss requested_actions genau eine Aktion enthalten.
- Bei completed, wait, blocked, fault und unclear muss requested_actions leer sein.
- Bei completed muss machine_state=erreicht sein.
- Bei completed muss completion_checks mindestens einen Eintrag enthalten.
- Jeder completion_check muss exakt die Felder symbol und value enthalten.
- Bei wait muss wait=true sein.
- Bei allen anderen Entscheidungen muss wait=false sein.
- wait_seconds darf nur bei wait groesser als 0.0 oder gleich 0.0 sein.
- Bei allen anderen Entscheidungen muss wait_seconds=0.0 sein.
- safe_state_required muss ein Boolean sein.
- read_only muss true sein, wenn requested_actions leer ist, und false, wenn requested_actions eine Aktion enthaelt.
- Behaupte niemals einen erfolgreichen Schritt, wenn Schreiben, Ruecklesen, Feedback oder Abschlusspruefung nicht erfolgreich waren.
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
      - Schlanker Prompt: _slim_snapshot() + _summarize_history()
      - Separater wait-Zaehler: wait-Wiederholungen brechen nicht mehr ab
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

    # ── Schlanker Snapshot fuer den Prompt (spart Tokens) ─────────────────────

    def _slim_snapshot(
        self, snapshot: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Reduziert den Snapshot auf die fuer die KI relevanten Felder.

        Entfernt data_type, description und timestamp – das spart bei
        grossen Maschinenbeschreibungen mehrere hundert Tokens pro Schritt.
        """
        return {
            sym: {
                "value": data.get("value"),
                "role":  data.get("role"),
                "valid": data.get("valid"),
            }
            for sym, data in snapshot.items()
        }

    # ── Komprimierte History fuer den Prompt (spart Tokens) ───────────────────

    def _summarize_history(self, n: int = 3) -> list[dict[str, Any]]:
        """Gibt die letzten n History-Eintraege komprimiert zurueck.

        Statt des vollstaendigen Step-Dicts (mit snapshot_before/after,
        llm_raw, etc.) wird nur das Wesentliche uebergeben.
        """
        result = []
        for entry in self.history[-n:]:
            event = entry.get("event", "")
            if event == "decision_received":
                dec = entry.get("decision", {})
                result.append({
                    "event":    event,
                    "step":     entry.get("step"),
                    "cycle":    entry.get("cycle"),
                    "decision": dec.get("decision"),
                    "summary":  dec.get("summary", ""),
                    "action":   dec.get("requested_actions", [{}])[0].get("symbol")
                                if dec.get("requested_actions") else None,
                })
            elif event == "step_executed":
                w = entry.get("write", {})
                result.append({
                    "event":    event,
                    "step":     entry.get("step"),
                    "cycle":    entry.get("cycle"),
                    "symbol":   w.get("symbol"),
                    "value":    w.get("requested"),
                    "write_ok": w.get("write_ok"),
                    "actual":   w.get("actual"),
                })
            elif event == "wait":
                result.append({
                    "event":   event,
                    "step":    entry.get("step"),
                    "cycle":   entry.get("cycle"),
                    "seconds": entry.get("seconds"),
                })
            # Alle anderen Events (cycle_completed, fault_*, etc.) kurz mitgeben
            else:
                result.append({
                    "event":   event,
                    "step":    entry.get("step"),
                    "cycle":   entry.get("cycle"),
                    "status":  entry.get("status"),
                    "message": entry.get("message", ""),
                    "errors":  entry.get("errors", [])[:5]
                                if isinstance(entry.get("errors", []), list)
                                else [],
                })
        return result

    # ── Prompt-Bau ────────────────────────────────────────────────────────────

    def _prompt(
        self,
        command: str,
        snapshot: dict[str, dict[str, Any]],
        cycle: int,
        limits: dict[str, Any] | None = None,
    ) -> str:
        n = int((limits or DEFAULT_LIMITS).get("prompt_history_entries", 3))
        # Maschinenbeschreibung: nur symbols und execution (kein agent-Block)
        machine_slim = {
            "name":        self.machine.get("name", ""),
            "description": self.machine.get("description", ""),
            "symbols":     self.machine.get("symbols", []),
            "execution":   self.machine.get("execution", {}),
        }
        payload = {
            "job_id":                   self.job_id,
            "original_command":         command,
            "cycle":                    cycle,
            "machine_description":      machine_slim,
            "snapshot_before_decision": self._slim_snapshot(snapshot),
            "previous_steps":           self._summarize_history(n),
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

        # read_only automatisch aus requested_actions ableiten.
        # Lokale Modelle (gemma etc.) setzen read_only haeufig inkonsistent
        # (z.B. read_only=true obwohl eine Aktion angefordert wird).
        # Da read_only kein Sicherheitsmerkmal ist (die eigentliche Freigabe
        # liegt in der Bedienerfreigabe und der Whitelist), wird es hier
        # deterministisch korrigiert statt die Antwort abzulehnen.
        if isinstance(response.get("requested_actions"), list) and response["requested_actions"]:
            response["read_only"] = False
        else:
            response["read_only"] = True

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
        elif response["decision"] == "completed" and not checks:
            errors.append("completed erfordert mindestens eine SPS-Abschlusspruefung")
        elif checks and response["decision"] not in {"completed", "wait"} and not all(
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
        elif len(actions) > 1:
            errors.append("Pro Entscheidung ist maximal eine Aktion erlaubt")
        elif actions:
            a = actions[0]
            if not isinstance(a, dict) or set(a) != {"symbol", "value", "reason"}:
                errors.append("Aktion muss exakt symbol, value und reason enthalten")
            elif not isinstance(a["symbol"], str) or not a["symbol"]:
                errors.append("Aktionssymbol ist ungueltig")
            elif not isinstance(a["reason"], str) or not a["reason"].strip():
                errors.append("Aktionsbegruendung darf nicht leer sein")

        decision = response["decision"]
        if decision in {"continue", "recover"} and len(actions) != 1:
            errors.append("continue muss genau eine Aktion enthalten")
        if decision not in {"continue", "recover"} and actions:
            errors.append("Nur continue darf eine Schreibaktion enthalten")
        # wait_seconds == 0.0 ist erlaubt: bedeutet Bedingungswarten / Polling.
        # Lokale Modelle setzen 0.0 wenn sie auf ein externes Signal warten
        # (z.B. Taster), ohne einen festen Timer zu kennen.
        # Die Anwendung ersetzt 0.0 intern durch das Polling-Intervall (1s).
        if decision == "wait" and response["wait_seconds"] < 0:
            errors.append("wait_seconds darf nicht negativ sein")
        if decision != "wait" and response["wait_seconds"] != 0:
            errors.append("wait_seconds darf nur bei wait gesetzt sein")
        if decision == "wait" and response["wait"] is not True:
            errors.append("wait muss bei decision=wait true sein")
        if decision != "wait" and response["wait"] is not False:
            errors.append("wait muss ausserhalb von wait false sein")
        # read_only-Konsistenzpruefung entfaellt, da read_only oben bereits
        # deterministisch aus requested_actions abgeleitet wird.

        return response, errors

    # ── Zeitstempel- und Konfidenzpruefung ────────────────────────────────────

    def _validate_timestamp_and_confidence(
        self, response: dict[str, Any], limits: dict[str, Any]
    ) -> list[str]:
        errors: list[str] = []

        # Zeitstempel-Pruefung:
        # Lokale Modelle (LM Studio) halluzinieren haeufig fest kodierte oder
        # veraltete Zeitstempel. Da der Zeitstempel bei einem lokalen,
        # nicht-vernetzten Modell keinen echten Sicherheitswert hat, wird er
        # nur noch auf grundlegende Gueltigkeit geprueft (parsebar + Zeitzone),
        # aber NICHT mehr auf Aktualitaet. Die Aktualitaet der Entscheidung
        # wird stattdessen durch den realen SPS-Snapshot sichergestellt.
        timestamp_check = self.machine.get("timestamp_check", "format")
        try:
            stamp = datetime.fromisoformat(
                str(response["timestamp"]).replace("Z", "+00:00")
            )
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                errors.append("Zeitstempel besitzt keine Zeitzone")
            elif timestamp_check == "age":
                # Nur aktivieren wenn timestamp_check: "age" in der
                # Maschinenkonfiguration gesetzt ist (z.B. fuer Cloud-Modelle)
                age = (
                    datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
                ).total_seconds()
                if age < -5:
                    errors.append("Zeitstempel liegt unzulaessig in der Zukunft")
                elif age > float(self.machine.get("max_response_age_seconds", 120)):
                    errors.append("LLM-Antwort ist veraltet")
        except (TypeError, ValueError):
            errors.append("Zeitstempel ist ungueltig oder nicht parsebar")

        if response["confidence"] < float(self.machine.get("min_confidence", 0.85)):
            errors.append("Konfidenz ist zu niedrig")
        if response["wait_seconds"] > float(limits["max_wait_seconds"]):
            errors.append("Wartezeit ueberschreitet das konfigurierte Limit")
        return errors

    # ── Ausfuehrungsbedingungen ───────────────────────────────────────────────

    def _execution_conditions(
        self,
        snapshot: dict[str, dict[str, Any]],
        ignore_active_faults: bool = False,
    ) -> list[str]:
        errors: list[str] = []
        execution = self.machine.get("execution", {})
        for symbol in execution.get("required_true", []):
            if snapshot.get(symbol, {}).get("value") is not True:
                errors.append(f"Freigabe fehlt: {symbol}")
        for symbol in execution.get("required_false", []):
            # Bei einer explizit konfigurierten Recovery-Aktion darf der
            # aktive Fehlermerker selbst noch TRUE sein. Genau dieser Zustand
            # soll durch den Reset beseitigt werden. Alle anderen erforderlichen
            # Verriegelungen bleiben weiterhin wirksam.
            if ignore_active_faults and symbol in self._configured_fault_symbols():
                continue
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

    # ── Recovery-Konfiguration ────────────────────────────────────────────────

    def _configured_fault_symbols(self) -> set[str]:
        symbols: set[str] = set()
        for item in self.machine.get("symbols", []):
            if isinstance(item, dict) and item.get("role") == "fault_signal":
                symbols.add(str(item.get("symbol")))
        agent = self.machine.get("agent", {})
        if isinstance(agent, dict):
            symbols.update(str(x) for x in agent.get("fault_symbols", []) or [])
        recovery = self.machine.get("fault_recovery", {})
        if isinstance(recovery, dict):
            symbols.update(str(x) for x in recovery.get("fault_symbols", []) or [])
        return symbols

    def _automatic_recovery_action(
        self, snapshot: dict[str, dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Liefert eine explizit konfigurierte Recovery-Aktion fuer einen aktiven Fehler.

        Wichtig: Die Anwendung wartet hier nicht auf eine weitere LLM-Antwort,
        nachdem das Modell bereits korrekt fault erkannt hat. Wenn ein
        schreibbares fault_ack-Symbol in der Maschinenbeschreibung vorhanden
        ist, wird es als deterministisch konfigurierte Recovery-Aktion genutzt.
        Es werden keine freien Symbolnamen erfunden.
        """
        if not self._fault_is_active(snapshot):
            return None

        recovery = self.machine.get("fault_recovery", {})
        if isinstance(recovery, dict):
            for item in recovery.get("actions", []) or []:
                if not isinstance(item, dict):
                    continue
                action = item.get("action", item)
                if not isinstance(action, dict):
                    continue
                symbol = action.get("symbol")
                value = action.get("value")
                spec = self._spec(symbol) if isinstance(symbol, str) else None
                if (
                    spec is not None
                    and spec.get("writable") is True
                    and self._is_configured_recovery_action(symbol, value)
                ):
                    return {
                        "symbol": symbol,
                        "value": value,
                        "reason": str(
                            action.get(
                                "reason",
                                "Konfigurierte Recovery-Aktion fuer aktive Stoerung",
                            )
                        ),
                    }

        # UI-kompatibler Fallback: role=fault_ack + writable=true.
        for item in self.machine.get("symbols", []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("role") != "fault_ack" or item.get("writable") is not True:
                continue
            symbol = item.get("symbol")
            values = item.get("allowed_values") or [True]
            if not isinstance(symbol, str):
                continue
            if any(_same_value(True, value) for value in values):
                return {
                    "symbol": symbol,
                    "value": True,
                    "reason": "Konfiguriertes fault_ack-Signal zur Fehlerquittierung",
                }
        return None

    def _fault_is_active(
        self, snapshot: dict[str, dict[str, Any]], action: dict[str, Any] | None = None
    ) -> bool:
        """Ermittelt aktive Stoerungen deterministisch aus der Maschinenkonfiguration.

        Unterstuetzte Konfigurationen:
          - role="fault_signal" und value=true
          - agent.fault_symbols=[...] als Rueckwaertskompatibilitaet
          - fault_recovery.fault_symbols=[...]
        """
        configured = self._configured_fault_symbols()

        # Eine Recovery-Aktion darf nur bei einer konfigurierten aktiven
        # Stoerung ausgefuehrt werden.
        for symbol in configured:
            state = snapshot.get(symbol, {})
            if state.get("valid") and state.get("value") is True:
                return True
        return False

    def _is_configured_recovery_action(
        self, symbol: str, value: Any
    ) -> bool:
        """Prueft, ob symbol/value explizit als Recovery freigegeben ist."""
        recovery = self.machine.get("fault_recovery", {})
        if not isinstance(recovery, dict):
            recovery = {}

        actions = recovery.get("actions", []) or []
        for item in actions:
            if not isinstance(item, dict):
                continue
            action = item.get("action", item)
            if not isinstance(action, dict):
                continue
            if action.get("symbol") == symbol and _same_value(action.get("value"), value):
                return True

        # Alternative kompakte Schreibweise im Symbol selbst.
        spec = self._spec(symbol)
        if isinstance(spec, dict):
            if spec.get("recovery") is True:
                recovery_values = spec.get("recovery_values", [True])
                if any(_same_value(value, allowed) for allowed in recovery_values):
                    return True

            # Die Weboberflaeche kennzeichnet ein Quittierungssignal als
            # role="fault_ack". Ist es dort schreibbar freigegeben, ist es
            # bereits eine ausdruecklich konfigurierte Recovery-Aktion.
            if spec.get("role") == "fault_ack" and spec.get("writable") is True:
                reset_values = spec.get("allowed_values")
                if reset_values:
                    return any(_same_value(value, allowed) for allowed in reset_values)
                return _same_value(value, True)

        return False

    def _recovery_feedback(self, symbol: str) -> list[dict[str, Any]]:
        """Liefert zusaetzliche Recovery-Rueckmeldungen aus der Konfiguration."""
        recovery = self.machine.get("fault_recovery", {})
        if not isinstance(recovery, dict):
            return []
        for item in recovery.get("actions", []) or []:
            if not isinstance(item, dict):
                continue
            action = item.get("action", item)
            if isinstance(action, dict) and action.get("symbol") == symbol:
                feedback = item.get("expected_feedback", [])
                return feedback if isinstance(feedback, list) else []
        return []

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
        recovery_role_allowed = (
            spec.get("role") in {"actuator", None}
            or (
                response.get("decision") == "recover"
                and spec.get("role") == "fault_ack"
            )
        )
        if spec.get("writable") is not True or not recovery_role_allowed:
            errors.append(f"Symbol nicht als schreibbarer Aktor oder Recovery-Signal freigegeben: {symbol}")
        if not current.get(symbol, {}).get("valid"):
            errors.append(f"Aktionssymbol ist im aktuellen Snapshot ungueltig: {symbol}")
        if not self._type_ok(spec, action["value"]):
            errors.append(f"Datentyp passt nicht: {symbol}")
        if response.get("decision") == "recover":
            if not self._fault_is_active(current, action):
                errors.append("Keine konfigurierte aktive Stoerung im aktuellen SPS-Snapshot")
            if not self._is_configured_recovery_action(symbol, action["value"]):
                errors.append(f"Recovery-Aktion nicht konfiguriert: {symbol}")
        allowed = spec.get("allowed_values")
        if allowed and not any(_same_value(action["value"], x) for x in allowed):
            errors.append(f"Wert nicht erlaubt: {symbol}")

        errors.extend(
            self._execution_conditions(
                current,
                ignore_active_faults=response.get("decision") == "recover",
            )
        )

        # Zustandsaenderungs-Check wurde entfernt.
        # Begruendung: Der Snapshot nach jedem Schreibvorgang wird bereits als
        # neue Vergleichsbasis gesetzt. Ein erneuter Vergleich zwischen
        # "snapshot nach letztem Schreiben" und "snapshot vor naechstem Schreiben"
        # wuerde legitime SPS-Reaktionen (z.B. Rueckmeldungen, Folgereaktionen)
        # als Fehler werten und den Ablauf faelschlicherweise abbrechen.
        # Die Sicherheit wird stattdessen durch folgende Mechanismen gewaehrleistet:
        #   - Frischer Snapshot direkt vor jedem Schreibvorgang
        #   - Whitelist-Pruefung (nur konfigurierte Symbole schreibbar)
        #   - Ruecklesen nach jedem Schreibvorgang
        #   - Feedback-Pruefung fuer konfigurierte Symbole
        self._prune_writes()
        if len(self.write_times) + 1 > int(limits["max_writes_per_minute"]):
            errors.append("Maximale Schreibfrequenz ueberschritten")

        return errors

    def _prune_writes(self) -> None:
        cutoff = time.monotonic() - 60.0
        self.write_times = [t for t in self.write_times if t >= cutoff]

    # ── Deterministischer Reset-Impuls ───────────────────────────────────────

    def _reset_pulse(
        self,
        action: dict[str, Any],
        spec: dict[str, Any],
        session: ProcessSession,
        limits: dict[str, Any],
        step: dict[str, Any],
    ) -> tuple[bool, str, dict[str, Any]]:
        """Fuehrt bei einem Recovery-Signal den Impuls TRUE -> FALSE aus.

        Beide Schreibvorgaenge werden einzeln validiert, geschrieben und
        zurueckgelesen. Der Impuls wird niemals ueber die KI geplant.
        """
        symbol = action["symbol"]
        data_type = spec["data_type"]
        pulse: dict[str, Any] = {
            "symbol": symbol,
            "set_value": True,
            "reset_value": False,
            "set_write_ok": False,
            "set_readback_ok": False,
            "reset_write_ok": False,
            "reset_readback_ok": False,
        }

        if session.total_write_count + 2 > int(limits["max_writes_per_job"]):
            return False, "Maximale Schreibanzahl fuer den Reset-Impuls ueberschritten", pulse

        self._prune_writes()
        if len(self.write_times) + 2 > int(limits["max_writes_per_minute"]):
            return False, "Maximale Schreibfrequenz fuer den Reset-Impuls ueberschritten", pulse

        ok, error = self.ads.write_value(symbol, data_type, True)
        self.write_times.append(time.monotonic())
        session.total_write_count += 1
        pulse["set_write_ok"] = bool(ok)
        pulse["set_error"] = error
        if not ok:
            return False, "ADS-Schreibfehler beim Setzen des Reset-Impulses", pulse

        actual, read_ok, read_error = self.ads.read_value(symbol, data_type)
        pulse["set_actual"] = _json_safe(actual)
        pulse["set_readback_ok"] = bool(read_ok)
        pulse["set_readback_error"] = read_error
        if not read_ok or not _same_value(actual, True):
            return False, "Ruecklesen des gesetzten Reset-Impulses fehlgeschlagen", pulse

        # Optional kurze Haltezeit aus der Konfiguration; Standard 0 Sekunden.
        recovery = self.machine.get("fault_recovery", {})
        hold_seconds = 0.0
        if isinstance(recovery, dict):
            hold_seconds = float(recovery.get("pulse_hold_seconds", 0.0) or 0.0)
        if hold_seconds > 0:
            self.sleep(min(hold_seconds, float(limits["max_wait_seconds"])))

        ok, error = self.ads.write_value(symbol, data_type, False)
        self.write_times.append(time.monotonic())
        session.total_write_count += 1
        pulse["reset_write_ok"] = bool(ok)
        pulse["reset_error"] = error
        if not ok:
            return False, "ADS-Schreibfehler beim Ruecksetzen des Reset-Impulses", pulse

        actual, read_ok, read_error = self.ads.read_value(symbol, data_type)
        pulse["reset_actual"] = _json_safe(actual)
        pulse["reset_readback_ok"] = bool(read_ok)
        pulse["reset_readback_error"] = read_error
        if not read_ok or not _same_value(actual, False):
            return False, "Reset-Impuls konnte nicht sicher auf FALSE zurueckgesetzt werden", pulse

        return True, "Reset-Impuls TRUE->FALSE erfolgreich ausgefuehrt", pulse

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
        """Fingerprint fuer Nicht-wait-Entscheidungen (continue, completed, etc.)."""
        return json.dumps(
            {
                "decision": response["decision"],
                "actions":  response["requested_actions"],
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    def _wait_fingerprint(self, response: dict[str, Any]) -> str:
        """Fingerprint fuer wait-Entscheidungen.

        wait-Wiederholungen sind legitim (Warten auf externes Signal) und
        werden separat mit einem hoeheren Limit gezaehlt.
        """
        return json.dumps(
            {
                "decision":    "wait",
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

        Wiederholungserkennung:
          - repeated:      Zaehlt identische Nicht-wait-Entscheidungen
                           (Limit: max_identical_decisions)
          - wait_repeated: Zaehlt identische wait-Entscheidungen separat
                           (Limit: max_identical_wait_decisions)
          Damit wird legitimes Warten auf ein externes Signal nicht
          faelschlicherweise als Fehler gewertet.

        Snapshot-Basis nach wait:
          Nach jedem abgeschlossenen Wartezustand wird 'snapshot' neu gelesen
          und als neue Vergleichsbasis fuer den Zustandsaenderungs-Check
          verwendet. Dadurch loest ein sich aenderndes Signal (z.B.
          .BAMPELANFORDERN wechselt auf true) keinen Abbruch aus.
        """
        steps: list[dict[str, Any]] = []
        writes_this_cycle = 0

        # Separate Zaehler fuer Wiederholungserkennung
        repeated: dict[str, int] = {}       # fuer continue/completed/blocked/etc.
        wait_repeated: dict[str, int] = {}  # fuer wait (eigenes, hoeheres Limit)
        llm_retries = 0
        prewrite_retries = 0
        recovery_attempts = 0

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

            # Gesamtzeitlimit
            if time.monotonic() - cycle_start > float(limits["cycle_timeout_seconds"]):
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
                self._prompt(command, snapshot, session.cycle_count, limits)
            )
            step: dict[str, Any] = {
                "step":            step_index,
                "cycle":           session.cycle_count,
                "started_at":      _now(),
                "snapshot_before": snapshot,
                "llm_raw":         raw if isinstance(raw, str) else repr(raw),
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
                llm_retries += 1
                step["status"] = "llm_response_rejected"
                step["errors"] = parse_errors
                step["response"] = response
                steps.append(step)

                self._emit({
                    "event":   "llm_response_rejected",
                    "job_id":  self.job_id,
                    "step":    step_index,
                    "cycle":   session.cycle_count,
                    "status":  "llm_response_rejected",
                    "message": (
                        "LLM-Antwort abgelehnt. Beim naechsten Versuch "
                        "ausschliesslich das vorgegebene JSON-Schema verwenden."
                    ),
                    "errors":  parse_errors,
                })

                if llm_retries <= int(limits["max_llm_retries"]):
                    # Kein Schreibvorgang wurde ausgefuehrt. Der aktuelle
                    # Snapshot bleibt unveraendert und die KI bekommt im
                    # naechsten Prompt die Ablehnungsdetails mitgeteilt.
                    continue

                detail = " | ".join(parse_errors) if parse_errors else "Unbekannter JSON-Fehler"
                session.steps.extend(steps)
                return {
                    "status":  "failed",
                    "message": (
                        "LLM-Antwort wurde nach begrenzten Korrekturversuchen "
                        "weiterhin abgelehnt: " + detail
                    ),
                    "errors": parse_errors,
                }

            # Eine gueltige Antwort setzt den reinen Formatfehler-Zaehler zurueck.
            llm_retries = 0
            step["response"] = response
            session.last_step_summary = response.get("summary", "")

            self._emit({
                "event":    "decision_received",
                "job_id":   self.job_id,
                "step":     step_index,
                "cycle":    session.cycle_count,
                "decision": response,
            })

            decision = response["decision"]

            # Wenn das Modell eine aktive Stoerung erkennt, aber fault statt
            # recover liefert, uebernimmt die Anwendung die bereits explizit
            # konfigurierte Recovery-Aktion. Dadurch endet der Auftrag nicht
            # mehr sofort im alten waiting_for_ack-Pfad.
            if decision == "fault":
                configured_recovery = self._automatic_recovery_action(snapshot)
                if configured_recovery is not None:
                    recovery_attempts += 1
                    if recovery_attempts <= int(limits["max_recovery_attempts"]):
                        response = dict(response)
                        response["decision"] = "recover"
                        response["requested_actions"] = [configured_recovery]
                        response["read_only"] = False
                        response["machine_state"] = "stoerung"
                        response["wait"] = False
                        response["wait_seconds"] = 0.0
                        response["summary"] = (
                            "Aktive Stoerung erkannt. Konfigurierte "
                            "Fehlerquittierung wird als TRUE->FALSE-Impuls ausgefuehrt."
                        )
                        step["response_original"] = step["response"]
                        step["response"] = response
                        self._emit({
                            "event":   "automatic_recovery_selected",
                            "job_id":  self.job_id,
                            "step":    step_index,
                            "cycle":   session.cycle_count,
                            "status":  "recover",
                            "message": response["summary"],
                            "action":  configured_recovery,
                        })
                        decision = "recover"

            if decision != "continue":
                prewrite_retries = 0

            # ── Wiederholungserkennung (getrennt nach wait / rest) ─────────
            if decision == "wait":
                # wait-Entscheidungen: separater Zaehler mit eigenem Limit
                wfp = self._wait_fingerprint(response)
                wait_repeated[wfp] = wait_repeated.get(wfp, 0) + 1
                if wait_repeated[wfp] > int(limits["max_identical_wait_decisions"]):
                    step["status"] = "failed"
                    step["errors"] = ["Maximale Wartewiederholungen ueberschritten"]
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Ablauf abgebrochen: Warten auf Signal hat das Zeitlimit ueberschritten.",
                    }
            else:
                # Alle anderen Entscheidungen: gemeinsamer Zaehler
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
                # wait_seconds == 0.0: KI wartet auf externes Signal (Taster,
                # Bedingung) ohne festen Timer. Anwendung pollt im 1s-Takt.
                if wait_secs <= 0.0:
                    wait_secs = 1.0
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

                # Snapshot nach wait neu lesen und als neue Vergleichsbasis
                # setzen. Dadurch loest eine erwartete Zustandsaenderung
                # (z.B. .BAMPELANFORDERN wird true) keinen Abbruch aus.
                snapshot, snap_errors = self._snapshot()
                if snap_errors:
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Snapshot nach Wartezustand unvollstaendig.",
                        "errors":  snap_errors,
                    }
                continue

            # ── continue / recover ─────────────────────────────────────────
            if decision in {"continue", "recover"}:
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
                    step["status"] = "action_rejected"
                    step["errors"] = validation_errors
                    step["snapshot_at_validation"] = current
                    steps.append(step)

                    self._emit({
                        "event":   "action_rejected",
                        "job_id":  self.job_id,
                        "step":    step_index,
                        "cycle":   session.cycle_count,
                        "status":  "action_rejected",
                        "message": (
                            "Die angeforderte Aktion wurde vor dem Schreiben "
                            "deterministisch abgelehnt."
                        ),
                        "errors":  validation_errors,
                    })

                    # Sicherheitsbedingungen werden niemals durch Retry umgangen.
                    # Ein Retry ist nur fuer korrigierbare Modellfehler zulaessig.
                    permanent_prefixes = (
                        "Schreibmodus ist",
                        "Maschinenbeschreibung ist",
                        "Freigabe fehlt:",
                        "Verriegelung aktiv",
                        "Betriebsart nicht freigegeben",
                        "Stopp wurde",
                        "Aktionssymbol ist im aktuellen Snapshot ungueltig:",
                        "Maximale Schreibfrequenz ueberschritten",
                    )
                    permanent_block = any(
                        any(error.startswith(prefix) for prefix in permanent_prefixes)
                        for error in validation_errors
                    )

                    if not permanent_block:
                        prewrite_retries += 1
                        if prewrite_retries <= int(limits["max_prewrite_retries"]):
                            snapshot, snapshot_errors = self._snapshot()
                            if snapshot_errors:
                                session.steps.extend(steps)
                                return {
                                    "status":  "failed",
                                    "message": "Snapshot nach abgelehnter Aktion unvollstaendig.",
                                    "errors":  snapshot_errors,
                                }
                            continue

                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": (
                            "Pruefung nicht bestanden. Keine Aktion ausgefuehrt: "
                            + " | ".join(validation_errors)
                        ),
                        "errors": validation_errors,
                    }

                prewrite_retries = 0
                action = response["requested_actions"][0]
                spec = self._spec(action["symbol"])

                # Ein fault_ack- oder explizit als Recovery markiertes Signal
                # ist ein Taster/Impuls: TRUE setzen, bestaetigen, danach
                # zwingend wieder FALSE setzen und bestaetigen.
                is_recovery_pulse = (
                    response.get("decision") == "recover"
                    and self._is_configured_recovery_action(
                        action["symbol"], action["value"]
                    )
                    and (
                        spec.get("role") == "fault_ack"
                        or spec.get("recovery") is True
                    )
                )

                if is_recovery_pulse:
                    pulse_ok, pulse_message, pulse_result = self._reset_pulse(
                        action, spec, session, limits, step
                    )
                    write_result: dict[str, Any] = {
                        "symbol": action["symbol"],
                        "requested": _json_safe(action["value"]),
                        "write_ok": pulse_ok,
                        "error": None if pulse_ok else pulse_message,
                        "reset_pulse": pulse_result,
                    }
                    step["write"] = write_result
                    writes_this_cycle += 2
                    if not pulse_ok:
                        step["status"] = "execution_error"
                        step["errors"] = [pulse_message]
                        self._emit({
                            "event":   "execution_error",
                            "job_id":  self.job_id,
                            "step":    step_index,
                            "cycle":   session.cycle_count,
                            "status":  "execution_error",
                            "message": pulse_message,
                            "errors":  [pulse_message],
                        })
                        steps.append(step)
                        session.steps.extend(steps)
                        return {
                            "status":  "failed",
                            "message": pulse_message,
                        }
                    ok = True
                    error = ""
                else:
                    ok, error = self.ads.write_value(
                        action["symbol"], spec["data_type"], action["value"]
                    )
                    self.write_times.append(time.monotonic())
                    session.total_write_count += 1
                    writes_this_cycle += 1

                    write_result = {
                        "symbol":    action["symbol"],
                        "requested": _json_safe(action["value"]),
                        "write_ok":  ok,
                        "error":     error,
                    }
                    step["write"] = write_result

                    if not ok:
                        step["status"] = "execution_error"
                        step["errors"] = ["ADS-Schreibfehler"]
                        self._emit({
                            "event":   "execution_error",
                            "job_id":  self.job_id,
                            "step":    step_index,
                            "cycle":   session.cycle_count,
                            "status":  "execution_error",
                            "message": "ADS-Schreibfehler. Keine weitere Aktion.",
                            "errors":  ["ADS-Schreibfehler"],
                        })
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
                        "actual":         _json_safe(actual),
                        "readback_ok":    read_ok,
                        "readback_error": read_error,
                    })
                    if not read_ok or not _same_value(actual, action["value"]):
                        step["status"] = "execution_error"
                        step["errors"] = ["Ruecklesen weicht vom Schreibwert ab"]
                        self._emit({
                            "event":   "execution_error",
                            "job_id":  self.job_id,
                            "step":    step_index,
                            "cycle":   session.cycle_count,
                            "status":  "execution_error",
                            "message": "Ruecklesen weicht vom Schreibwert ab.",
                            "errors":  ["Ruecklesen weicht vom Schreibwert ab"],
                        })
                        steps.append(step)
                        session.steps.extend(steps)
                        return {
                            "status":  "failed",
                            "message": "Ruecklesen weicht vom Schreibwert ab.",
                        }

                feedback_errors = self._feedback(spec)
                if response.get("decision") == "recover":
                    recovery_feedback = self._recovery_feedback(action["symbol"])
                    if recovery_feedback:
                        recovery_spec = dict(spec)
                        recovery_spec["expected_feedback"] = list(
                            spec.get("expected_feedback", []) or []
                        ) + recovery_feedback
                        feedback_errors.extend(self._feedback(recovery_spec))
                write_result["feedback_errors"] = feedback_errors
                if feedback_errors:
                    step["status"] = "execution_error"
                    step["errors"] = feedback_errors
                    self._emit({
                        "event":   "execution_error",
                        "job_id":  self.job_id,
                        "step":    step_index,
                        "cycle":   session.cycle_count,
                        "status":  "execution_error",
                        "message": "Erwartete Rueckmeldung blieb aus.",
                        "errors":  feedback_errors,
                    })
                    steps.append(step)
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Erwartete Sensorreaktion blieb aus.",
                    }

                # Snapshot nach Schreiben lesen und als neue Vergleichsbasis setzen
                snapshot, snap_errors = self._snapshot()
                step["snapshot_after"] = snapshot
                step["status"] = "executed"
                session.status = ProcessStatus.RUNNING

                if snap_errors:
                    step["status"] = "execution_error"
                    step["errors"] = snap_errors
                    steps.append(step)
                    self._emit({
                        "event":   "execution_error",
                        "job_id":  self.job_id,
                        "step":    step_index,
                        "cycle":   session.cycle_count,
                        "status":  "execution_error",
                        "message": "Snapshot nach Schreibvorgang unvollstaendig.",
                        "errors":  snap_errors,
                    })
                    session.steps.extend(steps)
                    return {
                        "status":  "failed",
                        "message": "Snapshot nach Schreibvorgang unvollstaendig.",
                        "errors":  snap_errors,
                    }

                steps.append(step)
                if response.get("decision") == "recover":
                    if not self._fault_is_active(snapshot):
                        recovery_attempts = 0

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
                        cycle_result["message"], overall_start
                    )

            if status == "fault":
                session.status = ProcessStatus.FAULT
                self._emit({
                    "event":   "fault_detected",
                    "job_id":  self.job_id,
                    "cycle":   session.cycle_count,
                    "message": cycle_result["message"],
                })
                # Dieser Fallback wird nur erreicht, wenn keine explizit
                # konfigurierte Recovery-Aktion vorhanden ist. Bei MAIN.bResetErr
                # als fault_ack sollte _run_cycle vorher automatisch recover
                # ausgefuehrt haben.
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
                session, ok, status, cycle_result["message"], overall_start
            )

    # ── Abschluss ─────────────────────────────────────────────────────────────

    def _finish_session(
        self,
        session: ProcessSession,
        ok: bool,
        status: str,
        message: str,
        overall_start: float,
    ) -> dict[str, Any]:
        try:
            session.status = ProcessStatus(status)
        except ValueError:
            session.status = ProcessStatus.FAILED
        session.finished_at = _now()
        elapsed = round(time.monotonic() - overall_start, 3)
        return {
            "ok":              ok,
            "status":          status,
            "job_id":          self.job_id,
            "message":         message,
            "cycle_count":     session.cycle_count,
            "step_count":      session.total_step_count,
            "write_count":     session.total_write_count,
            "elapsed_seconds": elapsed,
            "steps":           session.steps,
            "session":         session.public(),
        }
