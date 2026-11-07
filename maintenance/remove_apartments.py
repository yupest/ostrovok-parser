"""Remove confirmed apartments from one collection day, retaining source history in Git."""
import argparse,csv,json,sys
from datetime import date,timedelta
from pathlib import Path
from collections import Counter
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from accommodation_types import is_apartment

def read(path):
 with path.open(encoding='utf-8-sig',newline='') as f:
  reader=csv.DictReader(f);return list(reader),reader.fieldnames

def save(path,rows,fields):
 temporary=path.with_suffix('.tmp')
 with temporary.open('w',encoding='utf-8-sig',newline='') as f:
  writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore',lineterminator='\n');writer.writeheader();writer.writerows(rows)
 temporary.replace(path)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--collection-date',required=True);args=parser.parse_args()
 cache=json.loads((ROOT/'maintenance/kind_checks.json').read_text())
 catalog,fields=read(ROOT/'catalog/hotels.csv')
 for key in ['kind','monitoring_scope','card_status','card_checked_date','card_check_reason']:
  if key not in fields:fields.append(key)
 for h in catalog:
  check=cache.get(h['ota_hotel_id'],{})
  h['kind']=check.get('kind') or h.get('kind','')
  h['monitoring_scope']='excluded_apartment' if is_apartment(h['kind']) else 'included'
  if check:
   h.update(card_status=check['status'],card_checked_date=check['date'],card_check_reason=check['reason'])
   if check.get('url'):h['url']=check['url']
 by_id={h['ota_hotel_id']:h for h in catalog}
 excluded={key for key,h in by_id.items() if is_apartment(h['kind'])}
 report={'collection_date':args.collection_date,'stay_date':(date.fromisoformat(args.collection_date)+timedelta(days=1)).isoformat(),'catalog_types':dict(Counter(h['kind'] or 'UNKNOWN' for h in catalog)),'files':{}}
 # Keep a classification audit without duplicating the catalog.
 save(ROOT/'maintenance/property_type_audit.csv',[{'ota_hotel_id':h['ota_hotel_id'],'kind':h['kind'],'monitoring_scope':h['monitoring_scope'],'checked_date':h.get('card_checked_date','')} for h in catalog],['ota_hotel_id','kind','monitoring_scope','checked_date'])
 for folder in ['hotels','rooms','statistics']:
  path=ROOT/'daily'/folder/f'{args.collection_date}.csv';rows,columns=read(path)
  kept=[r for r in rows if r['ota_hotel_id'] not in excluded]
  if folder!='rooms':
   if 'kind' not in columns:columns.append('kind')
   for r in kept:r['kind']=by_id.get(r['ota_hotel_id'],{}).get('kind','')
  report['files'][folder]={'before':len(rows),'after':len(kept),'removed_rows':len(rows)-len(kept)}
  save(path,kept,columns)
 save(ROOT/'catalog/hotels.csv',catalog,fields)
 (ROOT/'maintenance'/f'cleanup-{args.collection_date}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(report,ensure_ascii=False,indent=2))
