from __future__ import annotations
import json, logging, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .ads_client import AdsClient
from .config import app_config, machine_config, save, APP_PATH, MACHINE_PATH
from .control_service import ControlService
from .llm_client import LlmClient

ROOT=Path(__file__).resolve().parents[1]; WEB=ROOT/'web'; log=logging.getLogger(__name__)

def normalize_machine(data):
    """Vereinheitlicht ältere Konfigurationen vor jeder Prüfung."""
    if not isinstance(data,dict):
        return data
    symbols=data.get('symbols',[])
    if isinstance(symbols,list):
        for spec in symbols:
            if isinstance(spec,dict) and spec.get('writable') is True:
                # Die sichtbare Schreibfreigabe ist die maßgebliche Richtung.
                # Dadurch bleiben ältere gespeicherte UI-Zustände kompatibel.
                spec['role']='actuator'
    return data

class State:
    def __init__(self): self.lock=threading.RLock(); self.app=app_config(); self.machine=normalize_machine(machine_config()); self.ads=None; self.symbols=[]; self.write_enabled=False; self.last_result=None; self.command_running=False; self.command_id=None
    def llm(self):
        c=self.app.get('llm',{}); return LlmClient(c.get('base_url','http://127.0.0.1:1234/v1'),c.get('model',''),float(c.get('timeout_seconds',120)),float(c.get('temperature',.1)),int(c.get('max_tokens',1200)),int(c.get('context_length',4096)))
    def public(self):
        return {'ads_connected':bool(self.ads and self.ads.connected),'write_enabled':self.write_enabled,'command_running':self.command_running,'command_id':self.command_id,'ads':self.app.get('ads',{}),'llm':self.app.get('llm',{}),'symbols':[x.__dict__ for x in self.symbols],'machine':self.machine,'last_result':self.last_result}
STATE=State()
def reply(h,data,status=200):
    body=json.dumps(data,ensure_ascii=False,default=str).encode(); h.send_response(status); h.send_header('Content-Type','application/json; charset=utf-8'); h.send_header('Content-Length',str(len(body))); h.end_headers(); h.wfile.write(body)
class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args): log.info(fmt,*args)
    def body(self): return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0')) or 0) or b'{}')
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/state': return reply(self,STATE.public())
        if path=='/api/command-status':
            with STATE.lock:
                return reply(self,{'ok':True,'running':STATE.command_running,'job_id':STATE.command_id,'result':STATE.last_result})
        if path=='/api/llm/check':
            ok,msg=STATE.llm().check(); return reply(self,{'ok':ok,'message':msg})
        if path=='/': path='/index.html'
        file=WEB/path.lstrip('/')
        if file.is_file():
            data=file.read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
        return reply(self,{'ok':False,'message':'Nicht gefunden'},404)
    def do_POST(self):
        path=urlparse(self.path).path
        try: data=self.body()
        except Exception as exc: return reply(self,{'ok':False,'message':f'Ungültige Anfrage: {exc}'},400)
        with STATE.lock:
            if path=='/api/connect':
                c=data.get('ads',data); STATE.app.setdefault('ads',{}).update(c); save(APP_PATH,STATE.app); STATE.ads=AdsClient(c.get('host',''),c.get('ams_net_id',''),int(c.get('port',851)),float(c.get('timeout_seconds',3)),int(c.get('notification_cycle_ms',100))); ok,msg=STATE.ads.connect(); return reply(self,{'ok':ok,'message':msg})
            if path=='/api/disconnect':
                if STATE.ads: STATE.ads.disconnect()
                STATE.write_enabled=False; return reply(self,{'ok':True})
            if path=='/api/symbols':
                if not STATE.ads or not STATE.ads.connected: return reply(self,{'ok':False,'message':'ADS nicht verbunden'},400)
                STATE.symbols,error=STATE.ads.read_all_symbols(); return reply(self,{'ok':not error,'message':error,'symbols':[x.__dict__ for x in STATE.symbols]})
            if path=='/api/machine':
                STATE.machine=normalize_machine(data)
                save(MACHINE_PATH,STATE.machine)
                return reply(self,{'ok':True,'machine':STATE.machine})
            if path=='/api/settings':
                # Einstellungen werden explizit gespeichert. Der periodische
                # Statusabruf darf keine Formulareingaben überschreiben.
                if isinstance(data.get('ads'),dict):
                    STATE.app.setdefault('ads',{}).update(data['ads'])
                if isinstance(data.get('llm'),dict):
                    STATE.app.setdefault('llm',{}).update(data['llm'])
                save(APP_PATH,STATE.app)
                return reply(self,{'ok':True,'ads':STATE.app.get('ads',{}),'llm':STATE.app.get('llm',{})})
            if path=='/api/write-enable':
                STATE.write_enabled=bool(data.get('enabled',False))
                return reply(self,{'ok':True,'write_enabled':STATE.write_enabled})
            if path=='/api/read':
                values=[]
                for s in STATE.machine.get('symbols',[]):
                    if STATE.ads and STATE.ads.connected:
                        value,ok,error=STATE.ads.read_value(s['symbol'],s['data_type']); values.append({'symbol':s['symbol'],'value':value if ok else None,'valid':ok,'error':error})
                return reply(self,{'ok':True,'values':values})
            if path=='/api/command':
                if not STATE.ads or not STATE.ads.connected:
                    return reply(self,{'ok':False,'message':'ADS nicht verbunden'},400)
                command=str(data.get('command','')).strip()
                if not command:
                    return reply(self,{'ok':False,'message':'Kein Befehl eingegeben.'},400)
                if STATE.command_running:
                    return reply(self,{'ok':False,'message':'Es läuft bereits ein Befehl.'},409)
                STATE.app.setdefault('llm',{}).update(data.get('llm',{})); save(APP_PATH,STATE.app)
                job_id=uuid.uuid4().hex
                ads=STATE.ads
                llm=STATE.llm()
                machine=dict(STATE.machine)
                write_enabled=STATE.write_enabled
                STATE.command_running=True
                STATE.command_id=job_id
                STATE.last_result=None

                def run_command():
                    try:
                        service=ControlService(ads,llm,machine)
                        result=service.execute(command,write_enabled)
                    except Exception as exc:
                        log.exception('Unerwarteter Fehler bei der Befehlsausführung')
                        result={'ok':False,'message':'Unerwarteter Fehler bei der Befehlsausführung. Keine Aktion ausgeführt.','errors':[str(exc)]}
                    with STATE.lock:
                        STATE.last_result=result
                        STATE.command_running=False

                threading.Thread(target=run_command,daemon=True).start()
                return reply(self,{'ok':True,'started':True,'job_id':job_id,'message':'Befehl wird geprüft und ausgeführt...'})
        return reply(self,{'ok':False,'message':'Nicht gefunden'},404)
def run():
    c=STATE.app.get('server',{}); host=c.get('host','127.0.0.1'); port=int(c.get('port',8080)); server=ThreadingHTTPServer((host,port),Handler); log.info('Weboberfläche gestartet: http://%s:%s',host,port); server.serve_forever()
