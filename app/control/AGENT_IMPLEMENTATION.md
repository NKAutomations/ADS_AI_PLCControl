# Mehrstufiger Steuerungskern – erster Umsetzungsschritt

Diese Lieferung ergänzt den bestehenden Web-POC additiv. Der vorhandene Einzelaktions-Service bleibt unverändert.

## Enthalten

- `agent_service.py`: begrenzter Auftrag mit mehreren KI-Entscheidungsschritten
- `test_agent_service.py`: Mock-Tests ohne echte ADS-Schreibzugriffe
- `agent_config.example.json`: konservative Limits

## Ablauf

1. ADS-Zustand vollständig lesen
2. aktuellen Auftrag und Zustand an das lokale LLM geben
3. JSON strikt prüfen
4. höchstens eine Schreibaktion je Entscheidung zulassen
5. Whitelist, Rolle, Datentyp, Werte, Freigaben und Verriegelungen prüfen
6. vor dem Schreiben den Zustand erneut lesen
7. schreiben und direkt zurücklesen
8. erwartetes Feedback prüfen
9. neuen Zustand lesen und in den nächsten Prompt aufnehmen
10. bis Abschluss oder kontrolliertem Abbruch wiederholen

## Noch nicht integriert

Der aktive Webserver verwendet weiterhin den bestehenden `ControlService`. In einem nächsten Schritt wird `/api/command` kontrolliert auf diesen neuen Service umgestellt und der laufende Auftrag über `/api/command-status` mit einzelnen Schritten veröffentlicht.

Bis dahin kann der Kern isoliert mit Mock-ADS und Mock-LLM geprüft werden. Es werden keine echten ADS-Schreibzugriffe ausgeführt.
