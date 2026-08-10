# ADS_KI_Maschinensteuerung

Eigenständiger Web-Proof-of-Concept für lokale TwinCAT-Steuerung über ADS und LM Studio.

Dieses Projekt ist **nicht vom alten Analyseprojekt abhängig**. Es übernimmt nur das fachliche Prinzip des ADS-Symbolbrowsers und des typisierten Lesens.

## Funktionen

- ADS-Verbindung mit Host, AMS Net ID und Port
- echte ADS-Verifikation
- ADS-Symbolbrowser
- typisierte ADS-Lesezugriffe
- Auswahl relevanter Variablen
- eigene Beschreibung je Variable
- Rolle: Sensor/Eingang, Aktor/Ausgang, Freigabe, Verriegelung, Betriebsart oder Zustand
- Schreibfreigabe je Aktor
- erwartete Sensorrückmeldung je Aktor
- lokale Kommunikation mit LM Studio
- strikte JSON-Prüfung der LLM-Antwort
- zentrale Whitelist für ADS-Schreibzugriffe
- Rücklesen nach dem Schreiben
- Prüfung der erwarteten Sensorreaktion
- einfacher Webserver ohne zusätzliche Webframeworks

## Installation

1. Python 3.11, 3.12 oder 3.13 installieren.
2. `INSTALL.bat` ausführen.
3. `START.bat` ausführen.
4. Browser öffnen: `http://127.0.0.1:8080`

## Grenzen

Nur für einen kontrollierten Proof of Concept an kleinen, nicht sicherheitskritischen Maschinen. Keine Sicherheitsfunktionen, Not-Aus-Funktionen, Motion-Control-Aufgaben, schnellen Regelkreise oder zeitkritischen Verriegelungen über die KI steuern.
