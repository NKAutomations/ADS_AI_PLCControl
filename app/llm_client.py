from __future__ import annotations

import httpx


SYSTEM_PROMPT = """Du bist die lokale Vorschlagskomponente einer TwinCAT-POC-Anwendung.
Du hast keinen ADS-Zugriff und darfst keine freien ADS-Befehle erzeugen.
Verwende ausschließlich Symbole und Werte aus der Maschinenbeschreibung.
Wenn die Eingabe unklar ist, Daten fehlen, eine Freigabe fehlt oder die Aktion
nicht eindeutig ist, fordere keine Schreibaktion an.
Antworte ausschließlich mit einem gültigen JSON-Objekt ohne Markdown und ohne
Codeblock. Die Python-Anwendung validiert deine Antwort vollständig.
"""


class LlmClient:
    def __init__(
        self,
        base_url,
        model,
        timeout_seconds=120,
        temperature=0.1,
        max_tokens=1200,
        context_length=4096,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.context_length = int(context_length)

    def check(self):
        try:
            with httpx.Client(timeout=5) as client:
                response = client.get(self.base_url + "/models")
                response.raise_for_status()
                ids = [item.get("id", "") for item in response.json().get("data", [])]
            return True, "LM Studio erreichbar. Modelle: " + (", ".join(ids) or "keine")
        except Exception as exc:
            return False, f"LM Studio nicht erreichbar: {exc}"

    def ask(self, prompt):
        """Backward-compatible one-shot request."""
        return self.ask_agent(prompt, SYSTEM_PROMPT)

    def ask_agent(self, prompt, system_prompt):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
            "n_ctx": self.context_length,
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(self.base_url + "/chat/completions", json=payload)
                response.raise_for_status()
                choices = response.json().get("choices", [])
                content = ""
                if choices:
                    content = choices[0].get("message", {}).get("content", "").strip()
                return (content, True) if content else ("LM Studio lieferte keine Antwort", False)
        except Exception as exc:
            return f"LLM-Fehler: {exc}", False
