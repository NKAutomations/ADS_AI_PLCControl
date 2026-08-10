import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.control_service import ControlService
class Ads:
    connected=True
    def __init__(self): self.v={'OUT':False,'PERMIT':True,'FAULT':False}
    def read_value(self,s,t): return self.v.get(s),s in self.v,''
    def write_value(self,s,t,v): self.v[s]=v;return True,''
class Llm:
    def ask(self,p): return json.dumps({'timestamp':'2099-01-01T00:00:00+00:00','read_only':False,'machine_state':'bereit','confidence':.95,'observations':[],'anomalies':[],'requested_actions':[{'symbol':'OUT','value':True,'reason':'test'}],'wait':False,'safe_state_required':False}),True
def test_disabled_is_rejected():
    m={'enabled':False,'min_confidence':.8,'max_response_age_seconds':15,'max_writes_per_minute':10,'symbols':[{'symbol':'OUT','data_type':'BOOL','role':'actuator','description':'out','writable':True},{'symbol':'PERMIT','data_type':'BOOL','role':'permission','description':'permit','writable':False},{'symbol':'FAULT','data_type':'BOOL','role':'interlock','description':'fault','writable':False}],'execution':{'required_true':['PERMIT'],'required_false':['FAULT']}}
    r=ControlService(Ads(),Llm(),m).execute('ausfahren',True); assert not r['ok']; assert 'deaktiviert' in r['errors'][0]
