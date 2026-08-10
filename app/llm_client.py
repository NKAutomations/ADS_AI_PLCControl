from __future__ import annotations
import httpx

SYSTEM_PROMPT = '''Du bist die lokale Vorschlagskomponente einer TwinCAT-POC-Anwendung.
Du hast keinen ADS-Zugriff und darfst keine freien ADS-Befehle erzeugen.
Verwende ausschließlich Symbole und Werte aus der Maschinenbeschreibung.
Wenn die Eingabe unklar ist, Daten fehlen, eine Freigabe fehlt oder die Aktion nicht eindeutig ist, setze requested_actions auf [] und wait auf true.
Antworte ausschließlich mit einem gültigen JSON-Objekt ohne Markdown und ohne Codeblock.
Pflichtformat:
{"timestamp":"ISO-8601 mit Zeitzone","read_only":false,"machine_state":"unbekannt|bereit|in_ausfuehrung|erreicht|stoerung|pruefen","confidence":0.0,"observations":[],"anomalies":[],"requested_actions":[{"symbol":"exakter Symbolname","value":true,"reason":"Begründung"}],"wait":false,"safe_state_required":false}
Die Python-Anwendung validiert deine Antwort vollständig.'''

class LlmClient:
    def __init__(self, base_url, model, timeout_seconds=120, temperature=.1, max_tokens=1200, context_length=4096):
        self.base_url=base_url.rstrip('/'); self.model=model; self.timeout_seconds=timeout_seconds; self.temperature=temperature; self.max_tokens=max_tokens; self.context_length=context_length
    def check(self):
        try:
            with httpx.Client(timeout=5) as client:
                response=client.get(self.base_url+'/models'); response.raise_for_status(); ids=[x.get('id','') for x in response.json().get('data',[])]
                return True, 'LM Studio erreichbar. Modelle: '+(', '.join(ids) or 'keine')
        except Exception as exc: return False, f'LM Studio nicht erreichbar: {exc}'
    def ask(self, prompt):
        payload={'model':self.model,'messages':[{'role':'system','content':SYSTEM_PROMPT},{'role':'user','content':prompt}],'temperature':self.temperature,'max_tokens':self.max_tokens,'stream':False,'n_ctx':self.context_length}
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response=client.post(self.base_url+'/chat/completions',json=payload); response.raise_for_status(); choices=response.json().get('choices',[]); content=choices[0].get('message',{}).get('content','').strip() if choices else ''
                return (content, True) if content else ('LM Studio lieferte keine Antwort', False)
        except Exception as exc: return f'LLM-Fehler: {exc}', False
