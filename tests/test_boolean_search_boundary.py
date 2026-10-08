"""Synthetic Boolean contract and untrusted broker boundary regressions."""
import asyncio
import copy
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp import schemas
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from test_exact_search_boundary import response


QUERY = {'all': ['selected'], 'any': ['full', 'text'], 'none': ['excluded']}


def boolean_response(**kwargs):
    value = response(**kwargs)
    value['scope']['query'] = copy.deepcopy(QUERY)
    value['scope']['matching_semantics'] = 'local_nfkc_casefold_whitespace_substring'
    value['provider_pages'] = 2
    value['branch_coverage'] = [
        {'index': 0, 'query': 'full', 'state': 'active', 'scanned_candidates': 2,
         'processed_candidates': value['processed_candidates'], 'provider_pages': 1},
        {'index': 1, 'query': 'text', 'state': 'active', 'scanned_candidates': 1,
         'processed_candidates': 0, 'provider_pages': 1}]
    return value


class BooleanSchemaTests(unittest.TestCase):
    def test_terminal_branches_are_closed_and_unprimed_or_has_no_body(self):
        active = boolean_response(cursor=None)
        unprimed = boolean_response(cursor=None,scanned=2,pages=1)
        unprimed['provider_pages']=1
        unprimed['branch_coverage'][0]['state']='provider_end_unverified'
        unprimed['branch_coverage'][1].update(state='interrupted',scanned_candidates=0,provider_pages=0)
        for value in (active,unprimed):
            with self.subTest(value=value),self.assertRaises(ValueError):
                schemas.SearchMessagesResponse.model_validate(value)

    def test_flat_positive_query_and_semantics(self):
        schema = schemas.SearchMessagesRequest.model_json_schema()
        self.assertIn('BooleanSearchQuery', schema.get('$defs', {}), 'bounded object query missing')
        req = schemas.SearchMessagesRequest(target=7, query=QUERY, mode='latest')
        self.assertEqual(req.query.model_dump(), QUERY)
        self.assertEqual(schemas.SearchMessagesRequest(target=7, query='stemmed query', mode='latest').query,
                         'stemmed query')
        value = boolean_response()
        self.assertEqual(len(schemas.SearchMessagesResponse.model_validate(value).branch_coverage), 2)

    def test_invalid_shape_terms_and_bounds_rejected(self):
        for query in ({}, {'none': ['x']}, {'all': []}, {'all': 'x'}, {'all': [True]},
                      {'all': [{'any': ['x']}]}, {'all': ['*']}, {'any': ['x'] * 5},
                      {'all': ['Ａ', 'a']}, {'any': ['a\t b', 'A B']},
                      {'all': ['a'], 'none': ['b'], 'raw': {}},
                      {'all': ['a', 'b', 'c'], 'any': ['d', 'e', 'f'], 'none': ['g', 'h', 'i']}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                schemas.SearchMessagesRequest(target=7, query=query, mode='latest')

    def test_normalization_and_same_message_membership(self):
        self.assertTrue(hasattr(schemas, 'BooleanSearchQuery'), 'Boolean query missing')
        from telegram_search_mcp.boolean_query import boolean_matches, boolean_seeds
        query = schemas.BooleanSearchQuery(all=['strasse', 'é'], any=['foo bar', 'c++'], none=['ban'])
        self.assertTrue(boolean_matches(query, 'Straße e\u0301 ＦＯＯ\t\nＢＡＲ'))
        self.assertFalse(boolean_matches(query, 'Straße é foo-bar'))
        self.assertFalse(boolean_matches(query, 'Straße é C++ urban'))
        self.assertFalse(boolean_matches(query, 'Straße C++'))
        self.assertEqual(boolean_seeds(query), ['foo bar', 'c++'])
        self.assertEqual(boolean_seeds(schemas.BooleanSearchQuery(all=['one', 'two'])), ['one'])

    def test_branch_mapping_counts_semantics_and_visible_predicates_are_checked(self):
        self.assertTrue(hasattr(schemas, 'BooleanSearchQuery'), 'Boolean query missing')
        cases = []
        bad=boolean_response(); bad['branch_coverage']=[]; cases.append(bad)
        bad=boolean_response(); bad['branch_coverage'][1]['query']='other'; cases.append(bad)
        bad=boolean_response(); bad['branch_coverage'][1]['index']=True; cases.append(bad)
        bad=boolean_response(); bad['branch_coverage'][0]['processed_candidates']=3; cases.append(bad)
        bad=boolean_response(); bad['branch_coverage'][0]['scanned_candidates']=1; cases.append(bad)
        bad=boolean_response(); bad['scope']['matching_semantics']='tdlib_lexical'; cases.append(bad)
        bad=boolean_response(); bad['results'][0]['message']['text']['value']='full excluded selected'; cases.append(bad)
        bad=response(); bad['branch_coverage']=boolean_response()['branch_coverage']; cases.append(bad)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                schemas.SearchMessagesResponse.model_validate(value)
        partial=boolean_response(); partial['results'][0].update(status='partial',coverage_complete=False,issues=['text_truncated'])
        partial['results'][0]['message']['text'].update(value='prefix',truncated=True)
        partial['page_complete']=False
        self.assertEqual(schemas.SearchMessagesResponse.model_validate(partial).results[0].status,'partial')


class BooleanProxyTests(unittest.TestCase):
    def test_branch_regressions_and_terminal_branch_reopening_fail_closed(self):
        self.assertTrue(hasattr(schemas, 'BooleanSearchQuery'), 'Boolean query missing')
        wall=float(int(time.time()))
        for mode in ('counter', 'reopen'):
            with patch('telegram_search_mcp.broker_client.time.time',return_value=wall):
                first=boolean_response()
                first['branch_coverage'][0]['state']='provider_end_unverified'
                second=boolean_response(mid=20,cursor='search_'+'b'*64,processed=2)
                second['scope']=copy.deepcopy(first['scope'])
                if mode=='counter':
                    second['branch_coverage'][0].update(state='provider_end_unverified',scanned_candidates=1,processed_candidates=1)
                    second['branch_coverage'][1].update(scanned_candidates=2,processed_candidates=1)
                values=[first,second]
                class Proxy(BrokerClient):
                    def _request(self,op,payload): return values.pop(0)
                proxy=Proxy(socket_path=Path('/unused'),policy=RuntimePolicy(enabled_capabilities=('search_messages',)))
                req=schemas.SearchMessagesRequest(target=7,query=QUERY,mode='latest',limit=1)
                a=proxy.search_messages(req)
                self.assertEqual(a.status,'page')
                b=proxy.search_messages(req.model_copy(update={'cursor':a.next_cursor}))
                self.assertEqual(b.stop_reason,'broker_unavailable')
                self.assertEqual(b.results,[])

    def test_sdk_accepts_native_object_and_rejects_nested_coercions(self):
        self.assertTrue(hasattr(schemas, 'BooleanSearchQuery'), 'Boolean query missing')
        from telegram_search_mcp.server import build_server
        class Service:
            def close(self): pass
            def search_messages(self,request):
                self.seen=request
                return schemas.SearchMessagesResponse(status='error',stop_reason='provider_error')
        service=Service(); server=build_server(service_factory=lambda:service,
            policy=RuntimePolicy(enabled_capabilities=('search_messages',)))
        async def run():
            await server.call_tool('search_messages',{'target':7,'query':QUERY,'mode':'latest'})
            self.assertEqual(service.seen.query.model_dump(),QUERY)
            literal = '{"any":["literal"]}'
            await server.call_tool('search_messages',{'target':7,'query':literal,'mode':'latest'})
            self.assertEqual(service.seen.query,literal, 'JSON-looking string must keep lexical semantics')
            for value in ({'all':'["x"]'},{'all':[{'text':'x'}]},{'none':['x']}):
                with self.assertRaises(Exception):
                    await server.call_tool('search_messages',{'target':7,'query':value,'mode':'latest'})
        asyncio.run(run())
