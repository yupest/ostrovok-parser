import tempfile
import unittest
from pathlib import Path
from datetime import date
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

from ostrovok_hotels import OstrovokHotelsDailyParser


def hotel(slug):
    return {'ota_hotel_id': slug, 'master_id': slug, 'static_vm': {'name': slug}}


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.parser = OstrovokHotelsDailyParser()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parser.current_dir = Path(self.directory.name)

    def state(self, ids=(), total=2, finished=True, errors=()):
        state = self.parser._new_attempt()
        self.parser._consume_search_response(state, 200, {'hotels': [hotel(i) for i in ids],
                'search_finished': finished, 'total_av_hotels': total})
        state['errors'].extend(errors)
        return state

    def test_url_has_no_type_filter(self):
        query = parse_qs(urlparse(self.parser._build_search_url(date(2026, 11, 8), date(2026, 11, 9))).query)
        self.assertNotIn('type_group', query)
        self.assertEqual(query['guests'], ['1'])

    def test_polling_empty_does_not_mean_end(self):
        state = self.parser._new_attempt()
        self.parser._consume_search_response(state, 200, {'hotels': None, 'search_finished': False, 'total_av_hotels': 0})
        self.assertFalse(state['finished'])
        self.parser._consume_search_response(state, 200, {'hotels': [hotel('one')], 'search_finished': False})
        self.parser._consume_search_response(state, 200, {'hotels': None, 'search_finished': True, 'total_av_hotels': 1})
        self.assertTrue(state['finished'])
        self.assertEqual(len(state['hotels']), 1)

    def test_non_json_api_error_and_missing_total_fail(self):
        for code, payload in [(503, None), (200, {}), (200, {'hotels': [], 'error': 'blocked'}),
                              (200, {'hotels': [], 'search_finished': True})]:
            state = self.parser._new_attempt()
            self.parser._consume_search_response(state, code, payload)
            self.assertTrue(state['errors'])
            self.assertFalse(state['finished'])

    def test_error_is_retried_and_totals_are_reconciled(self):
        self.parser._fetch_search_page = Mock(side_effect=[self.state(finished=False), self.state(['one']), self.state(['two'])])
        self.parser._parse_all_pages_with_pagination(Mock(), 'https://example.com/?search=yes')
        self.assertEqual(len(self.parser.all_hotels), 2)
        self.assertEqual(self.parser._fetch_search_page.call_count, 3)

    def test_empty_before_total_and_repeated_page_fail(self):
        for bad in [self.state(), self.state(['one'])]:
            self.parser._fetch_search_page = Mock(side_effect=[self.state(['one']), bad, bad, bad])
            with self.assertRaises(RuntimeError):
                self.parser._parse_all_pages_with_pagination(Mock(), 'https://example.com/')

    def test_reported_1346_with_1344_unique_and_confirmed_end(self):
        pages = [self.state([str(i) for i in range(page * 20, (page + 1) * 20)], total=1346)
                 for page in range(67)]
        # Two listings move between pages and are repeated, as in a changing ranking.
        pages[1] = self.state([str(i) for i in range(18, 38)], total=1346)
        pages.append(self.state([str(i) for i in range(1340, 1346)], total=1346))
        pages.extend([self.state(total=1346) for _ in range(3)])
        self.parser._fetch_search_page = Mock(side_effect=pages)
        self.parser._parse_all_pages_with_pagination(Mock(), 'https://example.com/')
        self.assertEqual(len(self.parser.all_hotels), 1344)
        self.assertEqual(self.parser.page_diagnostics[-1]['outcome'], 'completed_with_count_drift')
        self.assertEqual(self.parser.page_diagnostics[-1]['count_gap'], 2)

    def test_network_errors_after_short_page_are_not_eof(self):
        pages = [self.state(['a', 'b'], total=4), self.state(['c'], total=4)]
        pages.extend([self.state(total=4, errors=['HTTP 503']) for _ in range(3)])
        self.parser._fetch_search_page = Mock(side_effect=pages)
        with self.assertRaises(RuntimeError):
            self.parser._parse_all_pages_with_pagination(Mock(), 'https://example.com/')

    def test_large_gap_not_accepted_as_end(self):
        pages = [self.state(['a', 'b'], total=100), self.state(['c'], total=100)]
        pages.extend([self.state(total=100) for _ in range(3)])
        self.parser._fetch_search_page = Mock(side_effect=pages)
        with self.assertRaises(RuntimeError):
            self.parser._parse_all_pages_with_pagination(Mock(), 'https://example.com/')

    def test_explicit_completed_zero_is_valid(self):
        self.parser._fetch_search_page = Mock(return_value=self.state(total=0))
        self.parser._parse_all_pages_with_pagination(Mock(), 'https://example.com/')
        self.assertEqual(self.parser.all_hotels, [])

    def test_max_pages_is_error(self):
        self.parser.max_pages = 1
        self.parser._fetch_search_page = Mock(return_value=self.state(['one'], total=200))
        with self.assertRaises(RuntimeError):
            self.parser._parse_all_pages_with_pagination(Mock(), 'https://example.com/')

    def test_late_previous_page_response_ignored(self):
        page = Mock(); self.parser._setup_response_interceptor(page)
        callbacks = dict(call.args for call in page.on.call_args_list)
        old = self.parser._new_attempt(); self.parser._attempt = old
        request = Mock(method='POST', url='https://ostrovok.ru/hotel/search/v2/site/serp')
        callbacks['request'](request)
        current = self.parser._new_attempt(); self.parser._attempt = current
        response = Mock(request=request, status=200)
        response.json.return_value = {'hotels': [], 'search_finished': True, 'total_av_hotels': 0}
        callbacks['response'](response)
        self.assertFalse(current['finished'])


if __name__ == '__main__':
    unittest.main()
