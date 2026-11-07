import csv
import tempfile
import unittest
from pathlib import Path
from datetime import date
from unittest.mock import Mock, patch
from card_status import CardChecker, classify_card, reconcile_catalog
import ostrovok_statistic
from ostrovok_rooms import OstrovokRoomsDailyParser


class CardTests(unittest.TestCase):
    def test_card_identity_and_errors(self):
        page = Mock(status_code=200, url='https://ostrovok.ru/hotel/mid123/hotel/', text='<title>Отель</title><link rel="canonical" href="https://ostrovok.ru/hotel/mid123/hotel/">')
        self.assertEqual(classify_card(page,'123')[0],'active')
        self.assertEqual(classify_card(page,'999')[0],'unknown')
        self.assertEqual(classify_card(Mock(status_code=503),'123')[0],'unknown')
        self.assertEqual(classify_card(Mock(status_code=404),'123')[0],'inactive')
        page.text='<title>Just a moment...</title>'
        self.assertEqual(classify_card(page,'123')[0],'unknown')

    def test_fallback_prevents_false_deactivation(self):
        checker=CardChecker(); checker.session=Mock()
        checker.session.get.side_effect=[Mock(status_code=404),Mock(status_code=200,url='https://ostrovok.ru/hotel/mid123/h/',text='<title>Отель</title><link rel="canonical" href="https://ostrovok.ru/hotel/mid123/h/">')]
        self.assertEqual(checker({'ota_hotel_id':'h','master_id':'123','url':'https://ostrovok.ru/old'})[0],'active')

    def test_missing_active_unknown_and_inactive(self):
        catalog={x:{'ota_hotel_id':x,'card_status':'active'} for x in ['active','gone','error','seen']}
        catalog['already_gone']={'ota_hotel_id':'already_gone','card_status':'inactive'}
        checker=Mock(side_effect=[('active','ok',''),('inactive','404',''),('unknown','503','')])
        daily=reconcile_catalog(catalog,[{'ota_hotel_id':'seen'}],checker,'2026-11-07')
        rows={h['ota_hotel_id']:h for h in daily}
        self.assertEqual(checker.call_count,3)
        self.assertEqual(rows['active']['collection_status'],'assumed_full_missing_search')
        self.assertEqual(rows['error']['collection_status'],'card_check_error')
        self.assertNotIn('gone',rows)
        self.assertEqual(catalog['gone']['card_status'],'inactive')
        # Inactive stays skipped tomorrow, but a reappearance reactivates it.
        checker.reset_mock();checker.side_effect=None;checker.return_value=('active','ok','');reconcile_catalog(catalog,[{'ota_hotel_id':'gone'}],checker,'2026-11-08')
        self.assertEqual(catalog['gone']['card_status'],'active')

    def test_missing_rows_not_requested_as_room_offers(self):
        parser=OstrovokRoomsDailyParser();parser._search_hotel=Mock()
        for status in ['assumed_full_missing_search','card_check_error']:
            self.assertEqual(parser._process_hotel({'collection_status':status},date.today(),date.today()),[])
        parser._search_hotel.assert_not_called()

    def test_statistics_assumption_and_unknown_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);day='2026-11-07'
            for folder in ['hotels','rooms']: (root/'daily'/folder).mkdir(parents=True)
            with (root/'daily/hotels'/f'{day}.csv').open('w') as f:
                w=csv.DictWriter(f,fieldnames=['ota_hotel_id','name','rooms_number','collection_status','card_status']);w.writeheader()
                for key,status in [('full','assumed_full_missing_search'),('error','card_check_error'),('request_failed','search_found')]:
                    w.writerow(dict(ota_hotel_id=key,name=key,rooms_number=38,collection_status=status,card_status='active'))
            (root/'daily/rooms'/f'{day}.csv').write_text('ota_hotel_id,allotment,capacity,price_rub_min\n')
            with patch.object(ostrovok_statistic,'__file__',str(root/'ostrovok_statistic.py')):
                ostrovok_statistic.generate_statistics(date(2026,11,7))
            with (root/'daily/statistics'/f'{day}.csv').open(encoding='utf-8-sig') as f: rows={r['ota_hotel_id']:r for r in csv.DictReader(f)}
            self.assertEqual(float(rows['full']['available_rooms_percent']),0)
            self.assertEqual(rows['full']['free_rooms_amount'],'0')
            self.assertEqual(rows['error']['available_rooms_percent'],'')
            self.assertEqual(rows['request_failed']['available_rooms_percent'],'')

if __name__=='__main__': unittest.main()
