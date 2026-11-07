"""Recover provider kind from current cards; source snapshots are not changed here."""
import csv,json,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from card_status import CardChecker
path=ROOT/'catalog/hotels.csv'
with path.open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
cache_path=ROOT/'maintenance/kind_checks.json'
cache=json.loads(cache_path.read_text()) if cache_path.exists() else {}
today=datetime.now(ZoneInfo('Asia/Irkutsk')).date().isoformat()
local=threading.local()
def check(h):
 if not hasattr(local,'checker'):local.checker=CardChecker()
 row=dict(h);status,reason,url=local.checker(row)
 return row['ota_hotel_id'],{'kind':row.get('kind',''),'status':status,'reason':reason,'url':url,'date':today}
pending=[h for h in rows if not cache.get(h['ota_hotel_id'],{}).get('kind') and cache.get(h['ota_hotel_id'],{}).get('date')!=today]
print('TO_CHECK',len(pending),flush=True)
with ThreadPoolExecutor(max_workers=8) as pool:
 for i,f in enumerate(as_completed([pool.submit(check,h) for h in pending]),1):
  key,result=f.result();cache[key]=result
  if i%20==0 or i==len(pending):
   cache_path.write_text(json.dumps(cache,ensure_ascii=False,indent=2));print('CHECKED',i,'/',len(pending),flush=True)
cache_path.write_text(json.dumps(cache,ensure_ascii=False,indent=2))
from accommodation_types import is_apartment
counts={}
for h in rows:
 v=cache.get(h['ota_hotel_id'],{});kind=v.get('kind') or h.get('kind','');counts[kind]=counts.get(kind,0)+1
print('KINDS',json.dumps(counts,ensure_ascii=False),flush=True)
