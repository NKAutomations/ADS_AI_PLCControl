"""Prompt construction for one user command and one control decision."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any
from .models import MachineConfig

SYSTEM_PROMPT = """Du bist die Vorschlagskomponente einer lokalen TwinCAT-POC-Anwendung.

Du darfst niemals ADS verwenden und keine freien Befehle erzeugen. Du darfst nur Symbole und Werte aus der Maschinenbeschreibung verwenden. Deine komplette Antwort muss ein einzelnes gueltiges JSON-Objekt sein. Kein Markdown, kein Codeblock, kein zusaetzlicher Text.

Bewerte die Benutzereingabe zusammen mit dem aktuellen ADS-Snapshot. Wenn die Eingabe unklar ist, Daten fehlen, Freigaben fehlen oder eine Aktion nicht eindeutig aus der Maschinenbeschreibung folgt, setze requested_actions auf [] und machine_state auf pruefen oder warte=true. Setze safe_state_required=true, wenn ein unsicherer oder stoerungsbehafteter Zustand vorliegt.

read_only beschreibt die vorgeschlagene Entscheidung. Setze read_only=false nur, wenn eine konkrete Aktion eindeutig begruendet ist. Die Python-Anwendung validiert alle Felder und entscheidet endgueltig, ob geschrieben wird. Deine Antwort ist niemals selbst ein Schreibbefehl.

Erlaubtes JSON-Format:
{
  "timestamp": "ISO-8601 mit Zeitzone",
  "read_only": false,
  "machine_state": "unbekannt|bereit|in_ausfuehrung|erreicht|stoerung|pruefen",
  "confidence": 0.0,
  "observations": ["..."],
  "anomalies": ["..."],
  "requested_actions": [{"symbol": "exakt aus Beschreibung", "value": true, "reason": "..."}],
  "wait": false,
  "safe_state_required": false
}
"""

def build_control_prompt(cfg: MachineConfig, user_command: str, snapshot: dict[str, dict[str, Any]], history: list[dict[str, Any]]) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    machine = cfg.model_dump(mode="json")
    return "\n".join([
        "ENTSCHEIDUNGSZEITPUNKT=" + now,
        "BENUTZEREINGABE_BEGIN",
        user_command,
        "BENUTZEREINGABE_END",
        "MASCHINENBESCHREIBUNG_BEGIN",
        json.dumps(machine, ensure_ascii=False, separators=(",", ":")),
        "MASCHINENBESCHREIBUNG_END",
        "AKTUELLER_ADS_SNAPSHOT_BEGIN",
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        "AKTUELLER_ADS_SNAPSHOT_END",
        "LETZTE_EREIGNISSE_BEGIN",
        json.dumps(history[-30:], ensure_ascii=False, separators=(",", ":")),
        "LETZTE_EREIGNISSE_END",
        "Erzeuge jetzt ausschliesslich das definierte JSON-Objekt.",
    ])
