# ADS_KI_Maschinensteuerung

Eigenständiger Web-Proof-of-Concept für die KI-gestützte Steuerung von TwinCAT-SPSen über das ADS-Protokoll und ein lokales Large Language Model (LM Studio).

---

## Inhaltsverzeichnis

1. [Zweck und Funktionsumfang](#zweck-und-funktionsumfang)
2. [Architektur und wichtige Komponenten](#architektur-und-wichtige-komponenten)
3. [Voraussetzungen](#voraussetzungen)
4. [Installation](#installation)
5. [Konfiguration](#konfiguration)
6. [Start und Nutzung](#start-und-nutzung)
7. [PLC/ADS-spezifische Hinweise](#plcads-spezifische-hinweise)
8. [REST-API-Übersicht](#rest-api-übersicht)
9. [Entwicklungs- und Testhinweise](#entwicklungs--und-testhinweise)
10. [Bekannte Einschränkungen](#bekannte-einschränkungen)

---

## Zweck und Funktionsumfang

Das Projekt ermöglicht es, einer lokalen SPS (Beckhoff TwinCAT) über natürlichsprachliche Textbefehle Anweisungen zu erteilen. Ein lokal laufendes LLM (LM Studio) wertet den Befehl aus, liest den aktuellen Maschinenzustand über ADS aus und erzeugt eine JSON-Entscheidung mit den zu setzenden Ausgängen. Der Server validiert die Antwort strikt, schreibt nur auf ausdrücklich freigegebene Symbole und liest die erwartete Sensorrückmeldung zurück.

**Kernfunktionen:**

- ADS-Verbindung zu einer TwinCAT-SPS (Host, AMS Net ID, Port)
- Echtzeit-Verifikation der ADS-Verbindung
- ADS-Symbolbrowser (Lesen aller SPS-Symbole)
- Typisierte ADS-Lese- und Schreibzugriffe (`BOOL`, `INT`, `DINT`, `UINT`, `UDINT`, `REAL`, `LREAL`, `TIME`, `STRING`)
- Maschinenbeschreibung: Jedes Symbol erhält eine Rolle (`sensor`, `actuator`, `feedback`, `state`, `mode`, `permission`, `interlock`), eine textuelle Beschreibung und optional zulässige Werte
- Zentrale Schreib-Whitelist: Nur Symbole mit `role: actuator` und `writable: true` dürfen geschrieben werden
- LLM-gesteuerte Entscheidungslogik über `AgentControlService` mit mehreren Schritten pro Auftrag
- Strikte JSON-Validierung der LLM-Antwort via Pydantic-Modelle
- Freigabe- und Verriegelungsprüfung vor jedem Schreibvorgang
- Rücklesen nach dem Schreiben und Prüfung der erwarteten Sensorrückmeldung
- Ereignisgesteuerter Supervisor-Loop mit `loop_mode`, Fehlerquittierung (`fault_ack`) und Stopp-Signal
- Einfacher Webserver ohne externe Web-Frameworks (Python-Stdlib `ThreadingHTTPServer`)
- Single-Page-Weboberfläche (`web/index.html`)

---

## Architektur und wichtige Komponenten

```
ADS_AI_PLCControl/
├── app/
│   ├── main.py                  # Einstiegspunkt, startet den HTTP-Server
│   ├── server.py                # ThreadingHTTPServer, REST-API-Handler, Zustandsverwaltung
│   ├── ads_client.py            # ADS-Verbindung, Symbol-Lesen (pyads)
│   ├── ads/
│   │   └── writable_ads_client.py  # Additive Schreiberweiterung (write_by_name)
│   ├── llm_client.py            # HTTP-Client für LM Studio (OpenAI-kompatibler Endpunkt)
│   ├── config.py                # Laden/Speichern von app_config.json und machine_config.json
│   ├── control_service.py       # Einfacher One-Shot-Steuerungsdienst (Legacy)
│   └── control/
│       ├── agent_service.py     # AgentControlService: mehrstufiger Steuerungsagent
│       ├── models.py            # Pydantic-Datenmodelle (MachineConfig, ControlResponse, …)
│       ├── config.py            # Laden und Validieren der control_config.json
│       ├── prompt.py            # Systemprompt und LLM-Anfrage-Konstruktion
│       ├── validator.py         # Fail-Closed-Validierung der LLM-Antworten
│       ├── service.py           # Snapshot → LLM → Freigabe → Schreiben → Feedback
│       ├── process_session.py   # Lifecycle-Zustand eines laufenden Auftrags
│       ├── wakeup.py            # Wecklogik: Timer, Event-Polling, Quittierung
│       └── control_panel.py    # Optionales PySide6-Steuerpanel
├── config/
│   ├── app_config.json          # Server-, ADS- und LLM-Einstellungen (wird erzeugt)
│   ├── machine_config.json      # Aktive Maschinenbeschreibung (wird erzeugt/gespeichert)
│   ├── control_config.json      # Beispiel-Maschinenbeschreibung (Zylinder-POC)
│   ├── control_config.example.json  # Kommentierte Beispielkonfiguration
│   ├── agent_config.example.json    # Beispielkonfiguration mit Agent-Parametern
│   └── machine_config_ampel_example.json  # Ampel-Beispielkonfiguration
├── web/
│   └── index.html               # Single-Page-Weboberfläche
├── tests/
│   ├── test_core.py             # Unit-Tests für ControlService (ohne SPS/LLM)
│   ├── test_control_poc.py      # POC-Integrationstests
│   ├── test_agent_service.py    # Tests für AgentControlService
│   ├── test_agent_service_extended.py
│   └── test_process_session.py  # Tests für ProcessSession
├── requirements.txt
├── INSTALL.bat                  # Virtuelle Umgebung anlegen und Abhängigkeiten installieren
└── START.bat                    # Anwendung starten
```

**Wichtige Abhängigkeiten** (`requirements.txt`):

| Paket | Version | Zweck |
|---|---|---|
| `pyads` | `3.2.2` | ADS-Kommunikation mit TwinCAT |
| `httpx` | `>=0.27.0` | HTTP-Client für LM Studio |

Alle weiteren genutzten Module (`pydantic`, `json`, `threading`, `http.server`) sind Teil der Python-Standardbibliothek oder werden transient mitgezogen.

---

## Voraussetzungen

| Voraussetzung | Details |
|---|---|
| **Betriebssystem** | Windows (TwinCAT ADS-Route erforderlich) |
| **Python** | 3.11, 3.12 oder 3.13 |
| **TwinCAT** | TwinCAT 3 Runtime mit konfigurierter ADS-Route zum Zielrechner |
| **LM Studio** | Lokal laufende Instanz mit geladenem Modell, OpenAI-kompatibler REST-API |
| **ADS-Route** | `AMS Net ID` und Netzwerk-Erreichbarkeit zwischen PC und SPS müssen eingerichtet sein |

---

## Installation

1. **Python installieren** (3.11, 3.12 oder 3.13) – Python Launcher `py` muss im PATH sein.
2. **`INSTALL.bat` ausführen** – legt ein virtuelles Environment unter `.venv\` an und installiert alle Abhängigkeiten aus `requirements.txt`:

   ```bat
   INSTALL.bat
   ```

3. **Konfigurationsdateien anpassen** (siehe [Konfiguration](#konfiguration)).
4. **`START.bat` ausführen** – startet den Webserver:

   ```bat
   START.bat
   ```

5. **Browser öffnen:** [`http://127.0.0.1:8080`](http://127.0.0.1:8080)

Alternativ manuell starten (nach Aktivierung des venv):

```bat
.venv\Scripts\python.exe app\main.py
```

---

## Konfiguration

### `config/app_config.json` – Server, ADS und LLM

Wird beim ersten Start automatisch erzeugt. Kann über die Weboberfläche (`/api/settings`) oder direkt bearbeitet werden.

```json
{
  "server": {
    "host": "127.0.0.1",
    "port": 8080
  },
  "ads": {
    "host": "10.12.56.150",
    "ams_net_id": "10.12.59.45.1.1",
    "port": 851,
    "timeout_seconds": 3.0,
    "notification_cycle_ms": 100
  },
  "llm": {
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "qwen3-4b-instruct-2507-ggu",
    "timeout_seconds": 120.0,
    "temperature": 0.1,
    "max_tokens": 1200,
    "context_length": 4096
  }
}
```

| Parameter | Beschreibung |
|---|---|
| `server.host` / `server.port` | Adresse des lokalen Webservers |
| `ads.host` | IP-Adresse des TwinCAT-Rechners |
| `ads.ams_net_id` | AMS Net ID der Ziel-SPS (Format `a.b.c.d.e.f`) |
| `ads.port` | ADS-Port der TwinCAT-Runtime (Standard: `851`) |
| `ads.timeout_seconds` | Verbindungs-Timeout für ADS-Zugriffe |
| `ads.notification_cycle_ms` | Poll-Intervall für ADS-Benachrichtigungen in ms |
| `llm.base_url` | OpenAI-kompatibler API-Endpunkt von LM Studio |
| `llm.model` | Modell-ID, exakt wie in LM Studio konfiguriert |
| `llm.timeout_seconds` | HTTP-Timeout für LLM-Anfragen |
| `llm.temperature` | Sampling-Temperatur (empfohlen: `0.1` für deterministische Ausgaben) |
| `llm.max_tokens` | Maximale Antwortlänge in Tokens |
| `llm.context_length` | Kontextfenstergröße des Modells |

### `config/machine_config.json` / `config/control_config.json` – Maschinenbeschreibung

Die aktive Maschinenbeschreibung wird unter `config/machine_config.json` gespeichert und kann über `POST /api/machine` oder die Weboberfläche geladen werden. Als Ausgangspunkt eignen sich die mitgelieferten Beispieldateien:

- `config/control_config.json` – Zylinder-Beispiel (wird beim Start als Vorlage geladen)
- `config/control_config.example.json` – kommentierte Vorlage
- `config/agent_config.example.json` – Vorlage mit erweiterten Agent-Parametern
- `config/machine_config_ampel_example.json` – Ampelsteuerung als Beispiel

**Wichtige Felder:**

```json
{
  "version": "0.2.0-event-process-poc",
  "machine_name": "TwinCAT Zylinder POC",
  "enabled": true,
  "min_confidence": 0.85,
  "max_writes_per_minute": 30,
  "symbols": [
    {
      "symbol": "MAIN.bFreigabeKI",
      "data_type": "BOOL",
      "role": "permission",
      "description": "SPS-Freigabe für kontrollierten Schreibbetrieb"
    },
    {
      "symbol": "MAIN.bZylinderA1Ausfahren",
      "data_type": "BOOL",
      "role": "actuator",
      "description": "Ausfahranforderung Zylinder A1",
      "writable": true,
      "safe_value": false,
      "allowed_values": [false, true],
      "expected_feedback": [
        { "symbol": "MAIN.bZylinderA1Ausgefahren", "value": true, "timeout_seconds": 5.0 }
      ]
    }
  ],
  "execution": {
    "required_true": ["MAIN.bFreigabeKI"],
    "required_false": ["MAIN.bStoerung"],
    "mode_symbol": "MAIN.eBetriebsart",
    "allowed_modes": ["AUTO"],
    "mode_values": { "0": "MANUAL", "1": "AUTO" }
  }
}
```

| Feld | Beschreibung |
|---|---|
| `enabled` | Muss `true` sein, damit Schreibvorgänge ausgeführt werden |
| `min_confidence` | Minimale LLM-Konfidenz (0–1), unter der nicht geschrieben wird |
| `max_writes_per_minute` | Rate-Limiting für Schreibvorgänge |
| `symbols[].role` | `sensor`, `actuator`, `feedback`, `state`, `mode`, `permission`, `interlock` |
| `symbols[].writable` | Nur `true` bei `role: actuator` erlaubt Schreibzugriff |
| `symbols[].safe_value` | Sicherheitswert, auf den im Fehlerfall zurückgesetzt wird |
| `symbols[].allowed_values` | Whitelist zulässiger Schreibwerte |
| `symbols[].expected_feedback` | Erwartete Sensorreaktion nach dem Schreiben (Symbol + Wert + Timeout) |
| `execution.required_true` | Symbole, die `true` sein müssen (z. B. SPS-Freigabe) |
| `execution.required_false` | Symbole, die `false` sein müssen (z. B. Störungsmerker) |
| `execution.mode_symbol` | Symbol für Betriebsart-Prüfung |
| `execution.allowed_modes` | Erlaubte Betriebsarten (z. B. `["AUTO"]`) |

**Agent-Parameter** (in `agent`-Block, z. B. in `agent_config.example.json`):

| Parameter | Beschreibung |
|---|---|
| `max_steps` | Maximale Einzelschritte pro Auftrag |
| `max_writes_per_job` | Maximale Schreibvorgänge pro Auftrag |
| `job_timeout_seconds` | Gesamt-Timeout eines Auftrags |
| `loop_mode` | `true` = Dauerloop (Supervisor-Schleife) |
| `max_cycles` | Maximale Loop-Durchläufe (`0` = unbegrenzt) |
| `ack_timeout_seconds` | Timeout auf Fehlerquittierung |

---

## Start und Nutzung

### Startreihenfolge

1. TwinCAT-Runtime und ADS-Route prüfen (Verbindung vom PC zur SPS muss bestehen).
2. LM Studio lokal starten, Modell laden und den API-Server aktivieren.
3. `START.bat` ausführen.
4. Browser öffnen: [`http://127.0.0.1:8080`](http://127.0.0.1:8080)
5. In der Weboberfläche unter **Einstellungen** ADS-Parameter und LLM-URL/Modell eintragen.
6. **ADS verbinden** – der Server verifiziert die Verbindung.
7. Optional: **Symbole laden** (ADS-Symbolbrowser).
8. Maschinenbeschreibung laden oder bearbeiten (`/api/machine`).
9. **Schreibmodus aktivieren** (bewusste Freigabe in der UI).
10. Einen natürlichsprachlichen Textbefehl eingeben und absenden (z. B. `„Zylinder A1 ausfahren"`).
11. Fortschritt und Ergebnis im Protokoll prüfen.

### Auftrag stoppen

Ein laufender Auftrag kann über `POST /api/command/stop` oder die Weboberfläche gestoppt werden. Der laufende Einzelschritt wird noch abgeschlossen.

---

## PLC/ADS-spezifische Hinweise

- **ADS-Route**: Zwischen dem PC (auf dem dieser Server läuft) und der TwinCAT-SPS muss eine gültige ADS-Route eingerichtet sein. Die `ams_net_id` muss zur Ziel-SPS passen.
- **Port 851**: Standard-Port für die TwinCAT 3 Runtime. Abweichungen sind in `app_config.json` einstellbar.
- **Schreib-Whitelist**: Das LLM kann keine neue Whitelist erzeugen. Nur Symbole mit `role: actuator` und `writable: true` in der Maschinenbeschreibung dürfen beschrieben werden.
- **Freigabe-Prüfung**: Vor jedem Schreibvorgang werden alle `execution.required_true`- und `execution.required_false`-Symbole live ausgelesen. Fehlt die Freigabe oder ist eine Verriegelung aktiv, wird nicht geschrieben.
- **Betriebsart**: Ist `mode_symbol` konfiguriert, muss die aktuelle Betriebsart in `allowed_modes` enthalten sein.
- **Sensorrückmeldung**: Nach jedem Schreibvorgang wird das in `expected_feedback` definierte Symbol gepolt. Bleibt die Rückmeldung innerhalb des Timeouts aus, wird der Auftrag mit Fehler abgebrochen.
- **Datentypen**: Unterstützte Typen: `BOOL`, `INT`, `DINT`, `UINT`, `UDINT`, `REAL`, `LREAL`, `TIME`, `STRING`.
- **Fault-Handling**: Symbole mit `role: fault_signal` lösen einen Fehlerzustand aus. Symbole mit `role: fault_ack` werden zur Quittierung gepolt – die KI schreibt diese Symbole **nicht** selbst.

---

## REST-API-Übersicht

Alle Endpunkte sind unter `http://127.0.0.1:8080/api/` erreichbar.

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/api/state` | Gesamtzustand (ADS, LLM, Symbole, letztes Ergebnis) |
| `GET` | `/api/command-status` | Status des laufenden/letzten Auftrags |
| `GET` | `/api/llm/check` | LLM-Erreichbarkeit prüfen |
| `POST` | `/api/connect` | ADS verbinden (`{ "host": …, "ams_net_id": …, "port": … }`) |
| `POST` | `/api/disconnect` | ADS trennen |
| `POST` | `/api/symbols` | Alle SPS-Symbole lesen |
| `POST` | `/api/machine` | Maschinenbeschreibung laden/ersetzen |
| `POST` | `/api/settings` | ADS- und LLM-Einstellungen aktualisieren |
| `POST` | `/api/write-enable` | Schreibmodus aktivieren/deaktivieren (`{ "enabled": true }`) |
| `POST` | `/api/read` | Aktuelle Werte aller konfigurierten Symbole lesen |
| `POST` | `/api/command` | Neuen Auftrag starten (`{ "command": "Zylinder ausfahren" }`) |
| `POST` | `/api/command/stop` | Laufenden Auftrag stoppen |

---

## Entwicklungs- und Testhinweise

### Tests ausführen

Die Tests benötigen weder eine SPS noch LM Studio. Alle externen Abhängigkeiten werden durch einfache Mock-Klassen ersetzt.

```bat
.venv\Scripts\python.exe -m pytest tests\
```

oder einzelne Testdateien:

```bat
.venv\Scripts\python.exe -m pytest tests\test_core.py -v
.venv\Scripts\python.exe -m pytest tests\test_agent_service.py -v
.venv\Scripts\python.exe -m pytest tests\test_process_session.py -v
```

**Testdateien:**

| Datei | Inhalt |
|---|---|
| `tests/test_core.py` | Unit-Tests für `ControlService` (Freigabe, Validierung, Schreibpfad) |
| `tests/test_control_poc.py` | POC-Integrationstests |
| `tests/test_agent_service.py` | Tests für `AgentControlService` |
| `tests/test_agent_service_extended.py` | Erweiterte Agent-Tests |
| `tests/test_process_session.py` | Tests für `ProcessSession`-Lifecycle |

### Entwicklungshinweise

- `app/control/agents/` enthält ältere, nicht aktive Versionen des AgentControlService (Referenz).
- `app/control/agent_service.py.bak` und `*_notworking.py` sind ebenfalls Archivdateien.
- Die aktive Implementierung liegt in `app/control/agent_service.py`.
- Neue Maschinenbeschreibungen gegen `app/control/models.py` (Pydantic) validieren – `app/control/config.py` prüft auf doppelte Symbole und fehlende Referenzen.
- LLM-Prompts werden in `app/control/prompt.py` erzeugt.
- Die Validierungslogik ist in `app/control/validator.py` zentralisiert (fail-closed: im Zweifel wird nicht geschrieben).

---

## Bekannte Einschränkungen

- **Nur Windows**: TwinCAT ADS erfordert Windows. Die ADS-Bibliothek `pyads` setzt eine installierte TwinCAT-Runtime oder einen installierten ADS-Router voraus.
- **Kein Sicherheitssystem**: Dieser POC ist ausschließlich für kleine, nicht sicherheitskritische Maschinen und kontrollierte Tests geeignet. Folgendes darf **nicht** über die KI gesteuert werden:
  - Not-Aus- und Schutzfunktionen
  - Motion Control
  - Schnelle Regelkreise
  - Zeitkritische Verriegelungen
- **Einzelner Auftrag gleichzeitig**: Es kann immer nur ein Auftrag gleichzeitig laufen.
- **LLM-Abhängigkeit**: Das System setzt voraus, dass LM Studio lokal läuft und ein kompatibles Modell geladen ist. Netzwerkbasierte LLM-Endpunkte sind konfigurierbar, aber nicht offiziell getestet.
- **Keine Authentifizierung**: Der Webserver ist ohne Authentifizierung. Nur im lokalen Netzwerk oder auf dem Entwicklungsrechner betreiben.
- **Keine persistente Auftragshistorie**: Nach einem Neustart des Servers ist der Auftragsverlauf nicht mehr verfügbar.
- **Pydantic-Modell-Kompatibilität**: Die Maschinenbeschreibung muss strikt dem Schema in `app/control/models.py` entsprechen – zusätzliche Felder werden abgelehnt (`extra="forbid"`).
