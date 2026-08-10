# ADS_KI_Maschinensteuerung: POC-Integration

## Ziel

Der POC verarbeitet genau einen Textbefehl pro Klick. Die vorhandene ADS_KI_Analyse bleibt die Basis fuer Verbindung, Symbolbrowser, Lesen, Notifications, Historie und LM-Studio-Kommunikation. Der neue Schreibpfad liegt getrennt in einer zentralen ControlService-Kette.

Es gibt keinen Dauerbetrieb und keine direkte ADS-Kommunikation aus dem LLM-Modul.

## Dateien uebernehmen

- `app/control/models.py`: strikte JSON- und Maschinenmodelle
- `app/control/config.py`: Laden und Pruefen der Maschinenbeschreibung
- `app/control/prompt.py`: Systemprompt und POC-Anfrage
- `app/control/validator.py`: fail-closed Validierung
- `app/control/service.py`: Snapshot, LLM, Freigabe, Schreiben, Ruecklesen, Feedback
- `app/control/control_panel.py`: PySide6-Steuerpanel
- `app/ads/writable_ads_client.py`: additive write_by_name-Erweiterung
- `config/control_config.json`: projektspezifische Symbol- und Freigabebeschreibung
- `tests/test_control_poc.py`: Tests ohne SPS und ohne LM Studio

## Einbau in app/ui/main_window.py

1. Import ergaenzen:

```python
from app.ads.writable_ads_client import WritableAdsClient
from app.control.control_panel import ControlPanel
```

2. In `_on_connect` den Konstruktor ersetzen:

```python
self.ads_client = WritableAdsClient(
    host=host,
    ams_net_id=ams,
    port=port,
    timeout_seconds=float(self.cfg.get("ads", {}).get("timeout_seconds", 3.0)),
    notification_cycle_ms=int(self.cfg.get("ads", {}).get("notification_cycle_ms", 10)),
)
```

Die bestehende Verbindungs-, Verifikations-, Lese- und Notification-Logik bleibt unveraendert.

3. In `_build_right_tabs`, vor `return tabs` ergaenzen:

```python
self.control_panel = ControlPanel(
    ads_provider=lambda: self.ads_client,
    parent=self,
)
tabs.addTab(self.control_panel, "KI-Steuerung (POC)")
```

4. In `_on_disconnect` zuerst den Schreibmodus deaktivieren:

```python
if hasattr(self, "control_panel"):
    self.control_panel.write_enabled.setChecked(False)
```

## Konfiguration anpassen

`config/control_config.json` muss auf die realen TwinCAT-Symbole angepasst werden. Die Beispielnamen sind keine Behauptung ueber die SPS. Fuer jeden Sensor und Aktor sind mindestens Symbol, TwinCAT-kompatibler Datentyp, Rolle und Beschreibung einzutragen.

Ein Ausgang darf nur schreiben, wenn `role` gleich `actuator` und `writable` gleich `true` ist. Das LLM kann keine neue Whitelist erzeugen.

`expected_feedback` beschreibt die Sensorreaktion nach einem Schreibvorgang. Wenn keine Rueckmeldung innerhalb des Zeitlimits eintritt, meldet der POC einen Fehler und fuehrt keine weitere Aktion aus.

## Startreihenfolge

1. TwinCAT-Runtime und ADS-Route pruefen.
2. Reale Symbolnamen und Datentypen in `control_config.json` eintragen.
3. LM Studio lokal starten und Modell-ID in `config/config.json` eintragen.
4. Bestehende Anwendung starten.
5. ADS verbinden und Symbole laden.
6. SPS-Freigabe `required_true` und keine aktive Verriegelung sicherstellen.
7. Register `KI-Steuerung (POC)` oeffnen.
8. Schreibmodus bewusst aktivieren.
9. Einen einzelnen Textbefehl senden.
10. Ergebnis, Rueckmeldung und Protokoll pruefen.

## Sicherheitsgrenze des POC

Der POC ist nur fuer kleine, nicht sicherheitskritische Maschinen und kontrollierte Tests bestimmt. Not-Aus, Schutzfunktionen, schnelle Regelkreise, Motion Control und zeitkritische Verriegelungen bleiben in TwinCAT und Sicherheitskomponenten. Bei fehlenden Daten, LLM-Fehler, veralteter Antwort, niedriger Konfidenz, geaendertem Snapshot, fehlender Freigabe oder fehlender Rueckmeldung wird nicht geschrieben oder der Ablauf abgebrochen.
