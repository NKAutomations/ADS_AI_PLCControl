from __future__ import annotations
import json, time
from datetime import datetime, timezone

class ControlService:
    def __init__(self, ads, llm, machine): self.ads=ads; self.llm=llm; self.machine=machine; self.history=[]; self.write_times=[]
    def spec(self,symbol): return next((x for x in self.machine.get('symbols',[]) if x.get('symbol')==symbol),None)
    def snapshot(self):
        values={}; errors=[]
        for s in self.machine.get('symbols',[]):
            value,ok,error=self.ads.read_value(s['symbol'],s['data_type']); values[s['symbol']]={'value':value if ok else None,'data_type':s['data_type'],'role':s.get('role','state'),'description':s.get('description',''),'valid':ok,'timestamp':datetime.now(timezone.utc).isoformat(timespec='milliseconds')}
            if not ok: errors.append(f"{s['symbol']}: {error}")
        return values,errors
    def prompt(self,command,snapshot): return 'BENUTZEREINGABE:\n'+command+'\nDATEN:\n'+json.dumps({'machine':self.machine,'snapshot':snapshot,'last_events':self.history[-20:]},ensure_ascii=False,separators=(',',':'))
    def same(self,a,b): return type(a) is type(b) and a==b
    def type_ok(self,spec,value):
        t=spec['data_type']
        if t=='BOOL': return type(value) is bool
        if t in {'INT','DINT','UINT','UDINT','TIME'}: return type(value) is int
        if t in {'REAL','LREAL'}: return type(value) in {int,float} and type(value) is not bool
        if t=='STRING': return type(value) is str
        return False
    def execute(self,command,write_enabled):
        if not command.strip(): return {'ok':False,'message':'Kein Befehl eingegeben.'}
        if not self.ads.connected: return {'ok':False,'message':'ADS ist nicht verbunden.'}
        before,errors=self.snapshot()
        if errors: return {'ok':False,'message':'Snapshot unvollständig. Keine Aktion ausgeführt.','errors':errors}
        raw,llm_ok=self.llm.ask(self.prompt(command,before))
        if not llm_ok: return {'ok':False,'message':'LLM nicht verfügbar. Keine Aktion ausgeführt.','errors':[raw]}
        try: response=json.loads(raw)
        except json.JSONDecodeError as exc: return {'ok':False,'message':f'LLM-Antwort ist kein gültiges JSON: {exc.msg}'}
        errors=self.validate(response,before,write_enabled)
        if errors: return {'ok':False,'message':'Prüfung nicht bestanden. Keine Aktion ausgeführt.','errors':errors,'response':response}
        writes=[]
        for action in response['requested_actions']:
            spec=self.spec(action['symbol']); ok,error=self.ads.write_value(action['symbol'],spec['data_type'],action['value']); self.write_times.append(time.monotonic()); item={'symbol':action['symbol'],'requested':action['value'],'write_ok':ok,'error':error}
            if not ok: writes.append(item); return {'ok':False,'message':'ADS-Schreibfehler.','writes':writes,'response':response}
            actual,read_ok,read_error=self.ads.read_value(action['symbol'],spec['data_type']); item.update({'actual':actual,'readback_ok':read_ok,'readback_error':read_error})
            if not read_ok or not self.same(actual,action['value']): writes.append(item); return {'ok':False,'message':'Rücklesen weicht vom Schreibwert ab.','writes':writes,'response':response}
            feedback=self.feedback(spec); item['feedback_errors']=feedback; writes.append(item)
            if feedback: return {'ok':False,'message':'Sensorreaktion nicht erreicht.','writes':writes,'response':response}
        event={'timestamp':datetime.now(timezone.utc).isoformat(timespec='milliseconds'),'command':command,'response':response,'writes':writes}; self.history=(self.history+[event])[-20:]
        return {'ok':True,'message':'Befehl validiert, geschrieben und rückgemeldet.','writes':writes,'response':response}
    def validate(self,response,before,write_enabled):
        errors=[]; required={'timestamp','read_only','machine_state','confidence','observations','anomalies','requested_actions','wait','safe_state_required'}
        if not isinstance(response,dict): return ['Antwort muss ein JSON-Objekt sein']
        errors += [f'Pflichtfelder fehlen: {x}' for x in sorted(required-set(response))]; errors += [f'Zusätzliches Feld verboten: {x}' for x in sorted(set(response)-required)]
        if errors: return errors
        if self.machine.get('enabled') is not True: errors.append('Maschinenbeschreibung ist deaktiviert')
        if write_enabled is not True: errors.append('Schreibmodus ist in der Weboberfläche nicht freigegeben')
        if response['read_only'] is not False: errors.append('LLM hat read_only nicht auf false gesetzt')
        if response['wait'] is not False or response['safe_state_required'] is not False: errors.append('LLM fordert Warten oder sicheren Zustand an')
        if type(response['confidence']) not in {int,float} or isinstance(response['confidence'],bool) or not 0<=response['confidence']<=1: errors.append('Konfidenz ist ungültig')
        elif response['confidence'] < float(self.machine.get('min_confidence',.85)): errors.append('Konfidenz ist zu niedrig')
        try:
            stamp=datetime.fromisoformat(str(response['timestamp']).replace('Z','+00:00'))
            if stamp.tzinfo is None: errors.append('Zeitstempel besitzt keine Zeitzone')
            elif (datetime.now(timezone.utc)-stamp.astimezone(timezone.utc)).total_seconds()>float(self.machine.get('max_response_age_seconds',15)): errors.append('LLM-Antwort ist veraltet')
        except Exception: errors.append('Zeitstempel ist ungültig')
        self.write_times=[x for x in self.write_times if x>=time.monotonic()-60]
        if len(self.write_times)+len(response['requested_actions'])>int(self.machine.get('max_writes_per_minute',10)): errors.append('Maximale Schreibfrequenz überschritten')
        after,read_errors=self.snapshot(); errors += read_errors
        for symbol,state in before.items():
            if not after.get(symbol,{}).get('valid') or not self.same(state.get('value'),after[symbol].get('value')): errors.append(f'Anlagenzustand hat sich geändert: {symbol}')
        seen=set()
        for action in response['requested_actions']:
            if not isinstance(action,dict) or set(action)!={'symbol','value','reason'}: errors.append('Aktion muss exakt symbol, value und reason enthalten'); continue
            symbol=action['symbol']; spec=self.spec(symbol)
            if symbol in seen: errors.append(f'Symbol mehrfach angefordert: {symbol}'); seen.add(symbol)
            if spec is None: errors.append(f'Symbol nicht in Maschinenbeschreibung: {symbol}'); continue
            # Die Checkbox "Schreiben" ist die verbindliche Freigabe für
            # den Schreibbetrieb. Ältere gespeicherte Konfigurationen können
            # noch role="sensor" enthalten, obwohl writable=true gesetzt
            # wurde. Diese Kombination darf deshalb nicht verworfen werden.
            if spec.get('writable') is not True:
                errors.append(f'Symbol nicht als Ausgang freigegeben: {symbol}')
            if not self.type_ok(spec,action['value']):
                errors.append(f'Datentyp passt nicht: {symbol}')
            allowed=spec.get('allowed_values')
            # null oder [] bedeutet: keine zusätzliche Wert-Whitelist.
            # Eine leere Eingabe in der Weboberfläche darf nicht jeden Wert
            # ablehnen.
            if allowed and not any(self.same(action['value'],x) for x in allowed):
                errors.append(f'Wert nicht erlaubt: {symbol}')
        execution=self.machine.get('execution',{})
        for symbol in execution.get('required_true',[]):
            if after.get(symbol,{}).get('value') is not True: errors.append(f'Freigabe fehlt: {symbol}')
        for symbol in execution.get('required_false',[]):
            if after.get(symbol,{}).get('value') is not False: errors.append(f'Verriegelung aktiv: {symbol}')
        return errors
    def feedback(self,spec):
        errors=[]
        for expected in spec.get('expected_feedback',[]):
            target=self.spec(expected['symbol'])
            if target is None: errors.append(f'Feedbacksymbol nicht beschrieben: {expected["symbol"]}'); continue
            deadline=time.monotonic()+float(expected.get('timeout_seconds',5))
            while time.monotonic()<=deadline:
                value,ok,_=self.ads.read_value(expected['symbol'],target['data_type'])
                if ok and self.same(value,expected['value']): break
                time.sleep(float(expected.get('poll_interval_seconds',.1)))
            else: errors.append(f'Feedback nicht erreicht: {expected["symbol"]}={expected["value"]}')
        return errors
