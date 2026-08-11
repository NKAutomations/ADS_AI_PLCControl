# Integration des mehrstufigen Steuerungskerns

## Ersetzte aktive Dateien

- `app/server.py`
- `app/llm_client.py`

## Verhalten

`POST /api/command` startet weiterhin asynchron einen Auftrag. Der Auftrag wird jetzt durch `AgentControlService` abgearbeitet.

`GET /api/command-status` liefert während der Ausführung:

- `running`
- `job_id`
- `command`
- `progress.event`
- `progress.step`
- `progress.steps`
- nach Abschluss zusätzlich `result.steps`

## Rückwärtskompatibilität

- `/api/connect`
- `/api/disconnect`
- `/api/symbols`
- `/api/machine`
- `/api/settings`
- `/api/write-enable`
- `/api/read`
- `/api/command`
- `/api/command-status`

bleiben erhalten.

Der LLM-Client unterstützt weiterhin `ask(prompt)` und zusätzlich `ask_agent(prompt, system_prompt)`.

## Sicherheitsverhalten

Der Server übergibt die Schreibfreigabe als Momentaufnahme an den Auftrag. Der Agent selbst aktiviert keine Freigabe. Bei einem laufenden Auftrag kann kein zweiter Auftrag gestartet werden.
