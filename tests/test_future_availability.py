import unittest,json
from datetime import date
from unittest.mock import Mock
from future_availability import control_nights,FutureAvailabilityChecker,minimum_rate_price
from card_status import reconcile_catalog

class FutureTests(unittest.TestCase):
 def test_monday_tuesday_calendar_months(self):
  nights=control_nights(date(2026,11,9))
  self.assertEqual([a for a,b in nights],[date(2026,12,14),date(2027,1,11),date(2027,2,15)])
  self.assertTrue(all(a.weekday()==0 and b.weekday()==1 and (b-a).days==1 for a,b in nights))
  self.assertEqual(len(control_nights(date(2024,1,31))),3)
 def checker(self,payloads,status=200):
  session=Mock();session.headers={}
  responses=[]
  for payload in payloads:
   response=Mock(status_code=status);response.json.return_value=payload;responses.append(response)
  session.post.side_effect=responses
  return FutureAvailabilityChecker(session),session
 def test_second_date_available(self):
  check,session=self.checker([{'master_id':1,'rates':[]},{'master_id':1,'rates':[{}]},{'master_id':1,'rates':[]}])
  evidence=check({'ota_hotel_id':'h','master_id':'1'},date(2026,11,9))
  self.assertEqual(evidence['future_availability_status'],'available');self.assertEqual(session.post.call_count,3)
  self.assertEqual(len(json.loads(evidence['control_dates'])),3)
 def test_minimum_price_across_all_control_nights(self):
  def rate(price):return {'payment_options':{'payment_types':[{'amount':str(price),'currency_code':'RUB'}]}}
  check,session=self.checker([{'master_id':1,'rates':[rate(5000),rate(4000)]},{'master_id':1,'rates':[rate(3000)]},{'master_id':1,'rates':[rate(3500)]}])
  evidence=check({'ota_hotel_id':'h','master_id':'1'},date(2026,11,9))
  self.assertEqual(evidence['control_min_price'],3000)
  self.assertEqual(evidence['control_price_arrival'],'2027-01-11')
  self.assertEqual(len(json.loads(evidence['control_results'])),3)
 def test_price_ignores_other_currencies_and_invalid_values(self):
  rates=[{'payment_options':{'payment_types':[{'amount':'1','currency_code':'USD'},{'amount':'nan'},{'amount':'-2'},{'amount':'2500','currency_code':'RUB'}]}}]
  self.assertEqual(minimum_rate_price(rates),2500)
 def test_all_three_empty_excluded(self):
  check,session=self.checker([{'master_id':1,'rates':[]}]*3)
  self.assertEqual(check({'ota_hotel_id':'h','master_id':'1'},date(2026,11,9))['future_availability_status'],'unavailable')
 def test_null_rates_with_verified_identity_is_no_offers(self):
  check,session=self.checker([{'master_id':1,'rates':None}]*3)
  self.assertEqual(check({'ota_hotel_id':'h','master_id':'1'},date(2026,11,9))['future_availability_status'],'unavailable')
 def test_http_error_not_unavailable_or_full(self):
  check,session=self.checker([{}]*6,status=503)
  self.assertEqual(check({'ota_hotel_id':'h','master_id':'1'},date(2026,11,9))['future_availability_status'],'error')
 def test_wrong_hotel_not_accepted(self):
  check,session=self.checker([{'master_id':2,'rates':[{}]}]*6)
  self.assertEqual(check({'ota_hotel_id':'h','master_id':'1'},date(2026,11,9))['future_availability_status'],'error')
 def test_no_future_offers_not_assumed_full(self):
  catalog={'h':{'ota_hotel_id':'h','master_id':'1','kind':'HOTEL'}}
  daily=reconcile_catalog(catalog,[],Mock(return_value=('active','ok','')),'2026-11-08',Mock(return_value={'future_availability_status':'unavailable'}))
  self.assertEqual(daily[0]['collection_status'],'no_future_offers')
  self.assertEqual(catalog['h']['card_status'],'active')

if __name__=='__main__':unittest.main()
