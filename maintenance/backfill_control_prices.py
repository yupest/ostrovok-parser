"""Add current control-night minimum tariffs to already verified catalog entries."""
import csv,sys,threading,json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from future_availability import FutureAvailabilityChecker,CONTROL_FIELDS
p=ROOT/'catalog/hotels.csv'
with p.open(encoding='utf-8-sig') as f:
 reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
selected=[r for r in rows if r.get('future_availability_status')=='available' and r.get('monitoring_scope')!='excluded_apartment' and r.get('card_status')=='active']
arrival=datetime.now(ZoneInfo('Asia/Irkutsk')).date()+timedelta(days=1)
local=threading.local();evidence={}
def check(h):
 if not hasattr(local,'checker'):local.checker=FutureAvailabilityChecker()
 return h['ota_hotel_id'],local.checker(h,arrival)
print('TO_CHECK',len(selected),flush=True)
with ThreadPoolExecutor(max_workers=6) as pool:
 for i,f in enumerate(as_completed([pool.submit(check,h) for h in selected]),1):
  key,value=f.result();evidence[key]=value
  if i%10==0 or i==len(selected):print('CHECKED',i,'/',len(selected),flush=True)
for key in CONTROL_FIELDS:
 if key not in fields:fields.append(key)
for row in rows:
 if row['ota_hotel_id'] in evidence:row.update(evidence[row['ota_hotel_id']])
with p.with_suffix('.tmp').open('w',encoding='utf-8-sig',newline='') as f:
 writer=csv.DictWriter(f,fieldnames=fields,lineterminator='\n');writer.writeheader();writer.writerows(rows)
p.with_suffix('.tmp').replace(p)
print('WITH_PRICE',sum(bool(v.get('control_min_price')) for v in evidence.values()),flush=True)
