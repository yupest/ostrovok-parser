"""Control-night availability for the agreed occupancy heuristic."""
import calendar,json,uuid,math
from datetime import date,timedelta
from urllib.parse import urlparse
import requests

CONTROL_FIELDS=['future_availability_status','availability_checked_date','control_dates','control_results','control_min_price','control_price_arrival']
ASSUMED_FULL='assumed_full_future_available'
EXCLUDED_STATUSES={'no_future_offers','future_check_error','card_check_error'}

def control_nights(arrival):
    result=[]
    for offset in (1,2,3):
        index=arrival.year*12+arrival.month-1+offset
        year,month=divmod(index,12);month+=1
        day=date(year,month,min(arrival.day,calendar.monthrange(year,month)[1]))
        monday=day+timedelta(days=(-day.weekday())%7)
        result.append((monday,monday+timedelta(days=1)))
    return result


def minimum_rate_price(rates):
    values=[]
    for rate in rates or []:
        for payment in rate.get('payment_options',{}).get('payment_types',[]) or []:
            for amount,currency in [('amount','currency_code'),('show_amount','show_currency_code')]:
                if payment.get(currency) not in (None,'','RUB'):continue
                try:
                    value=float(payment.get(amount))
                    if value>0 and math.isfinite(value):values.append(value)
                except (TypeError,ValueError):pass
    return min(values) if values else None


class FutureAvailabilityChecker:
    def __init__(self,session=None):
        self.session=session or requests.Session()
        self.session.headers.update({'User-Agent':'Mozilla/5.0','Accept':'application/json',
                                     'Origin':'https://ostrovok.ru','Referer':'https://ostrovok.ru/'})

    def __call__(self,hotel,arrival):
        slug=urlparse(hotel.get('url','')).path.rstrip('/').split('/')[-1] or hotel['ota_hotel_id']
        nights=control_nights(arrival);results=[]
        status='unavailable'
        for checkin,checkout in nights:
            result={'arrival':checkin.isoformat(),'departure':checkout.isoformat()}
            for attempt in range(2):
                try:
                    response=self.session.post('https://ostrovok.ru/hotel/search/v1/site/hp/search',json={
                        'hotel':slug,'arrival_date':checkin.isoformat(),'departure_date':checkout.isoformat(),
                        'currency':'RUB','lang':'ru','region_id':965821539,'paxes':[{'adults':1}],
                        'search_uuid':str(uuid.uuid4())},timeout=25)
                    result['http_status']=response.status_code
                    payload=response.json() if response.status_code==200 else {}
                    identity=str(payload.get('master_id','')).removesuffix('.0')
                    expected=str(hotel.get('master_id','')).removesuffix('.0')
                    valid=(response.status_code==200 and 'rates' in payload and (payload['rates'] is None or isinstance(payload['rates'],list))
                           and not payload.get('error') and not payload.get('err')
                           and (identity==expected if expected else payload.get('ota_hotel_id')==slug))
                    if valid:
                        result.update(status='available' if payload['rates'] else 'unavailable',rates=len(payload['rates'] or []))
                        if payload['rates']:
                            result['min_price']=minimum_rate_price(payload['rates'])
                        break
                    result['status']='error'
                except (requests.RequestException,ValueError,TypeError):result['status']='error'
            results.append(result)
            if result['status']=='available':
                status='available'
        if status!='available' and any(r['status']=='error' for r in results):status='error'
        prices=[r for r in results if r.get('status')=='available' and r.get('min_price') is not None]
        cheapest=min(prices,key=lambda r:r['min_price']) if prices else {}
        return {'control_min_price':cheapest.get('min_price',''),'control_price_arrival':cheapest.get('arrival',''),
                'future_availability_status':status,'availability_checked_date':(arrival-timedelta(days=1)).isoformat(),
                'control_dates':json.dumps([a.isoformat() for a,b in nights]),
                'control_results':json.dumps(results,ensure_ascii=False)}
