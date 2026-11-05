"""Negative GPT policy paths through real isolated gateway, ledger and account DBs."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.test_gpt_import_endpoint as F
from core import account_runtime as R, gpt_request_policy as P, gpt_runtime as G, scheduler
from core.adapter import get_adapter
from core.credit import credit
from core.pool import Pool
from core.sse import StreamIdleTimeout, FirstOutputTimeout
from adapters.openai_compat import UpstreamError


class GPTPolicyDelivery(unittest.TestCase):
    setUpClass = classmethod(F.GPTEndpointTest.setUpClass.__func__)
    setUp = F.GPTEndpointTest.setUp
    value = F.GPTEndpointTest.value

    def accounts(self, count=1):
        self.client.post('/api/admin/gpt/import', headers=self.admin, json=self.value())
        row = self.pool.list_accounts('gpt')[0]
        self.pool.update_account(row['id'], priority=0)
        for index in range(1, count):
            aid = self.pool.upsert_account('gpt', 'fallback-' + str(index), secret=row['secret'], meta=row['meta'], status='active')
            self.pool.update_account(aid, token=row['token'], token_exp=row['token_exp'], priority=index)
        return self.pool.list_accounts('gpt')

    def request(self, path, *, stream=False, key=None):
        body = {'model': 'gpt-5.6-luna', 'stream': stream}
        body.update({'input': 'hi'} if path == '/v1/responses' else {'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 64})
        return self.client.post(path, headers={'Authorization': 'Bearer ' + (key or F.config.API_KEY)}, json=body)

    def reset(self):
        def clear(meta):
            meta.pop('gpt_rate_limits', None)
            meta.pop('pool_upstream_usage', None)
        for row in self.pool.list_accounts('gpt'):
            self.pool.update_account(row['id'], status='active', temp_until=0, upstream_until=0)
            G.edit_meta(self.pool, row['id'], clear)

    def clock(self):
        return mock.patch.object(P, 'time', SimpleNamespace(sleep=mock.Mock(), time=time.time, monotonic=time.monotonic))

    def test_idle_timeout_without_output_never_spends_balance(self):
        self.accounts()
        user = self.users.get_user_by_email('user@gmail.com')
        self.users.update_group(user['group_id'], billing_policy='balance')
        credit(user['id'], 1, 'admin', 'gpt-idle-policy-credit')
        self.users.upsert_pricing('gpt-5.6-luna', None, billing_mode='token', input_price=1, output_price=2)
        key = self.client.post('/api/keys', headers=self.user, json={'name': 'timeout-test'}).json()['key']
        for path in ('/v1/responses', '/v1/chat/completions', '/v1/messages'):
            for stream in (False, True):
                before = self.users.get_user_by_email('user@gmail.com')['balance']
                with self.subTest(path=path, stream=stream), mock.patch('core.account_runtime.urlopen', side_effect=StreamIdleTimeout('upstream stream idle timeout: no model output')):
                    response = self.request(path, stream=stream, key=key)
                self.assertEqual(response.status_code, 200 if stream else 504, response.text)
                log = self.users.recent_usage(user['id'], limit=1)[0]
                self.assertEqual(log['cost'], 0)
                self.assertEqual(log['actual_cost'], 0)
                self.assertEqual(log['output_tokens'], 0)
                self.assertEqual(self.users.get_user_by_email('user@gmail.com')['balance'], before)

    def test_bad_request_does_not_park_any_account_or_replay(self):
        rows = self.accounts(2)
        for path in ('/v1/responses', '/v1/chat/completions', '/v1/messages'):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream), mock.patch('core.account_runtime.urlopen', side_effect=UpstreamError(400, 'invalid_request_error: unknown parameter invalid_grant')) as upstream:
                    response = self.request(path, stream=stream)
                self.assertEqual(response.status_code, 200 if stream else 400, response.text)
                self.assertEqual(upstream.call_count, 1)
                self.assertTrue(all(self.pool.get_account(row['id'])['status'] == 'active' for row in rows))

    def test_auth_rejection_is_recorded_and_healthy_fallback_answers(self):
        rows = sorted(self.accounts(2), key=lambda row: row['priority'])
        for path in ('/v1/responses', '/v1/chat/completions', '/v1/messages'):
            for stream in (False, True):
                self.reset()
                with self.subTest(path=path, stream=stream), mock.patch('core.account_runtime.urlopen', side_effect=[UpstreamError(403, 'account disabled'), io.BytesIO(F.wire())]) as upstream:
                    response = self.request(path, stream=stream)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('pong', response.text)
                self.assertEqual(upstream.call_count, 2)
                self.assertEqual(self.pool.get_account(rows[0]['id'])['status'], 'cooldown')
                self.assertEqual(self.pool.get_account(rows[1]['id'])['status'], 'active')

    def test_capacity_budget_then_healthy_account_for_all_protocols(self):
        self.accounts(2)
        for path in ('/v1/responses', '/v1/chat/completions', '/v1/messages'):
            for stream in (False, True):
                self.reset()
                errors = [UpstreamError(503, 'Our servers are currently overloaded') for _ in range(F.config.GPT_CAPACITY_RETRIES + 1)]
                with self.subTest(path=path, stream=stream), self.clock(), mock.patch('core.account_runtime.urlopen', side_effect=errors + [io.BytesIO(F.wire())]) as upstream:
                    response = self.request(path, stream=stream)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('pong', response.text)
                self.assertEqual(upstream.call_count, F.config.GPT_CAPACITY_RETRIES + 2)
                self.assertTrue(all(row['status'] == 'active' for row in self.pool.list_accounts('gpt')))

    def test_transient_429_retries_same_account_without_persisting_limit(self):
        row = self.accounts()[0]
        for path in ('/v1/responses', '/v1/chat/completions', '/v1/messages'):
            error = UpstreamError(429, 'temporarily rate limited')
            error.headers = {'Retry-After': '.01'}
            with self.subTest(path=path), self.clock(), mock.patch('core.account_runtime.urlopen', side_effect=[error, io.BytesIO(F.wire())]) as upstream:
                response = self.request(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(upstream.call_count, 2)
            self.assertFalse(self.pool.get_account(row['id'])['meta'].get('gpt_rate_limits'))
            self.assertNotIn((self.pool.path, row['id']), P._RATE_RETRIES)

    def test_failover_records_one_charge_attributed_to_the_answering_account(self):
        rows = sorted(self.accounts(2), key=lambda row: row['priority'])
        user = self.users.get_user_by_email('user@gmail.com')
        self.users.update_group(user['group_id'], billing_policy='balance')
        credit(user['id'], 1, 'admin', 'gpt-failover-policy-credit')
        self.users.upsert_pricing('gpt-5.6-luna', None, billing_mode='token', input_price=1, output_price=2, cache_read_price=.1)
        key = self.client.post('/api/keys', headers=self.user, json={'name': 'failover-test'}).json()['key']
        for path in ('/v1/responses', '/v1/chat/completions', '/v1/messages'):
            before = self.users.get_user_by_email('user@gmail.com')['balance']
            count = len(self.users.recent_usage(user['id'], limit=1000))
            errors = [UpstreamError(503, 'Our servers are currently overloaded') for _ in range(F.config.GPT_CAPACITY_RETRIES + 1)]
            with self.subTest(path=path), self.clock(), mock.patch('core.account_runtime.urlopen', side_effect=errors + [io.BytesIO(F.wire())]):
                response = self.request(path, stream=True, key=key)
            self.assertIn('pong', response.text)
            logs = self.users.recent_usage(user['id'], limit=1000)
            self.assertEqual(len(logs), count + 1)
            self.assertEqual(logs[0]['upstream_account_id'], rows[1]['id'])
            self.assertAlmostEqual(logs[0]['cost'], (5 + 14 + .6) / 1e6)
            self.assertAlmostEqual(before - self.users.get_user_by_email('user@gmail.com')['balance'], logs[0]['cost'])

    def test_partial_output_is_never_replayed_on_another_account(self):
        self.accounts(2)
        upstream_bytes = b'data: {"type":"response.output_text.delta","delta":"partial answer"}\n\ndata: {"type":"response.failed","response":{"error":{"code":"server_is_overloaded"}}}\n\n'
        for path in ('/v1/responses', '/v1/chat/completions', '/v1/messages'):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream), mock.patch('core.account_runtime.urlopen', side_effect=lambda *a, **kw: io.BytesIO(upstream_bytes)) as upstream:
                    response = self.request(path, stream=stream)
                self.assertEqual(upstream.call_count, 1)
                self.assertEqual(response.status_code, 200 if stream else 503, response.text)

    def test_first_output_timeout_switches_at_most_once(self):
        self.accounts(3)
        for path in ('/v1/responses', '/v1/chat/completions'):
            with self.subTest(path=path), mock.patch('core.account_runtime.urlopen', side_effect=FirstOutputTimeout('first output timed out')) as upstream:
                response = self.request(path)
            self.assertEqual(response.status_code, 504, response.text)
            self.assertEqual(upstream.call_count, 2)
            self.assertTrue(all(row['status'] == 'active' for row in self.pool.list_accounts('gpt')))

    def test_websocket_auth_failover_and_transient_rate_limit_retry(self):
        from tests.test_gpt_import import complete_response
        rows = sorted(self.accounts(2), key=lambda row: row['priority'])
        for row in rows:
            G.edit_meta(self.pool, row['id'], lambda meta: meta.update(gpt_settings={'ws_mode': 'ctx_pool'}))
        class Socket:
            closed = False
            def __init__(self, frames):
                self.frames = iter(frames)
            def settimeout(self, timeout):
                pass
            def send(self, data):
                pass
            def recv_data(self, control_frame=False):
                del control_frame  # 签名由 websocket-client 基类定死,刻意不用
                return 1, json.dumps(next(self.frames))
            def close(self):
                self.closed = True
        sources = [Socket([{'type': 'error', 'error': {'code': 'permission_denied', 'message': 'account disabled'}}]),
                   Socket([{'type': 'error', 'error': {'code': 'rate_limit_exceeded', 'message': 'temporarily busy'}}]),
                   Socket([{'type': 'response.completed', 'response': complete_response()}])]
        with self.clock(), mock.patch('core.gpt_native.open_ws', side_effect=sources) as connect:
            with self.client.websocket_connect('/v1/responses', headers={'Authorization': 'Bearer ' + F.config.API_KEY}) as socket:
                socket.send_json({'type': 'response.create', 'model': 'gpt-5.6-luna', 'input': 'hi'})
                result = socket.receive_json()
                self.assertEqual(result['type'], 'response.completed', result)
        self.assertEqual(connect.call_count, 3)
        self.assertTrue(all(source.closed for source in sources))
        self.assertEqual(self.pool.get_account(rows[0]['id'])['status'], 'cooldown')
        self.assertEqual(self.pool.get_account(rows[1]['id'])['status'], 'active')

    def test_exhausted_quota_switches_without_same_account_retry_and_recovers(self):
        rows = sorted(self.accounts(2), key=lambda row: row['priority'])
        error = UpstreamError(429, 'usage limit reached')
        error.headers = {'x-codex-primary-used-percent': '100', 'x-codex-primary-reset-after-seconds': '60'}
        with self.clock() as clock, mock.patch('core.account_runtime.urlopen', side_effect=[error, io.BytesIO(F.wire())]) as upstream:
            response = self.request('/v1/responses')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(upstream.call_count, 2)
        clock.sleep.assert_not_called()
        blocked = self.pool.get_account(rows[0]['id'])
        self.assertFalse(G.eligible(self.pool, blocked, 'gpt-5.6-luna'))
        with mock.patch.object(G.time, 'time', return_value=time.time() + 62):
            self.assertTrue(G.eligible(self.pool, blocked, 'gpt-5.6-luna'))

    def test_transient_429_window_expires_without_one_extra_request(self):
        row = self.accounts()[0]
        ticks = [0.0]
        def sleep(seconds):
            ticks[0] += seconds
        clock = SimpleNamespace(monotonic=lambda: ticks[0], time=time.time, sleep=sleep)
        with mock.patch.object(P, 'time', clock), mock.patch.object(P, 'OAUTH_429_RETRY_WINDOW', .02), mock.patch('core.account_runtime.urlopen', side_effect=UpstreamError(429, 'temporarily rate limited')) as upstream:
            response = self.request('/v1/responses')
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(upstream.call_count, 1)
        current = self.pool.get_account(row['id'])
        self.assertEqual(current['status'], 'active')
        self.assertLessEqual(current['meta']['gpt_rate_limits']['gpt-5.6-luna'] - time.time(), 6)

    def test_quota_without_reset_uses_short_fallback_instead_of_stale_snapshot_ban(self):
        row = self.accounts()[0]
        error = UpstreamError(429, 'usage limit reached')
        error.headers = {'x-codex-primary-used-percent': '100'}
        with mock.patch('core.account_runtime.urlopen', side_effect=error) as upstream:
            response = self.request('/v1/responses')
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(upstream.call_count, 1)
        row = self.pool.get_account(row['id'])
        with mock.patch.object(G.time, 'time', return_value=time.time() + 7):
            self.assertTrue(G.eligible(self.pool, row, 'gpt-5.6-luna'))

    def test_429_has_three_account_budget_and_other_failures_obey_switch_setting(self):
        self.accounts(4)
        def busy(*args, **kwargs):
            raise UpstreamError(429, 'temporarily busy')
        for path in ('/v1/responses', '/v1/chat/completions'):
            self.reset()
            with self.subTest(path=path, error=429), mock.patch.object(P, 'OAUTH_429_RETRY_WINDOW', 0), mock.patch('core.account_runtime.urlopen', side_effect=busy) as upstream:
                response = self.request(path)
            self.assertEqual(response.status_code, 429, response.text)
            self.assertEqual(upstream.call_count, 3)
            self.reset()
            with self.subTest(path=path, error=502), mock.patch.object(F.config, 'GPT_MAX_ACCOUNT_SWITCHES', 1), mock.patch('core.account_runtime.urlopen', side_effect=UpstreamError(502, 'bad gateway')) as upstream:
                response = self.request(path)
            self.assertEqual(response.status_code, 502, response.text)
            self.assertEqual(upstream.call_count, 2)

    def test_shadow_account_shares_parent_concurrency_and_cleans_timed_out_waiter(self):
        row = self.accounts()[0]
        self.pool.update_account(row['id'], concurrency=1)
        child = self.pool.upsert_account('gpt', 'shadow-capacity', meta={'parent_account_id': row['id']}, status='active')
        with R.dispatch(self.pool, self.pool.get_account(row['id']), record=False):
            self.assertTrue(R.saturated(self.pool.get_account(child), self.pool))
            with self.assertRaises(R.AccountTestTimeout), R.test_budget(.02), R.dispatch(self.pool, self.pool.get_account(child), record=False):
                self.fail('a shadow account cannot bypass the parent concurrency')
            self.assertEqual(R.capacity(row['id']), 1)
            self.assertEqual(R.capacity(child), 0)
            self.assertNotIn(row['id'], R._WAITING)

    def test_queue_rejects_requests_above_waiting_limit_and_drains_existing_waiter(self):
        row = self.accounts()[0]
        self.pool.update_account(row['id'], concurrency=1)
        row = self.pool.get_account(row['id'])
        queued = threading.Event()
        wait = R._CAPACITY_CHANGED.wait
        def observed_wait(timeout):
            queued.set()
            return wait(timeout)
        def request():
            with R.dispatch(self.pool, row, record=False):
                return True
        with ThreadPoolExecutor(max_workers=1) as workers, mock.patch.object(F.config, 'GPT_ACCOUNT_MAX_WAITING', 1), mock.patch.object(R._CAPACITY_CHANGED, 'wait', side_effect=observed_wait):
            with R.dispatch(self.pool, row, record=False):
                pending = workers.submit(request)
                self.assertTrue(queued.wait(2))
                with self.assertRaises(R.AccountBusyError), R.dispatch(self.pool, row, record=False):
                    self.fail('the account waiting limit must be enforced')
                self.assertEqual(R._WAITING[row['id']], 1)
            self.assertTrue(pending.result(timeout=2))
        self.assertEqual(R.capacity(row['id']), 0)
        self.assertNotIn(row['id'], R._WAITING)

    def test_expired_credentials_refresh_once_and_try_another_account(self):
        rows = sorted(self.accounts(2), key=lambda row: row['priority'])
        self.pool.update_account(rows[0]['id'], token_exp=1)
        with mock.patch.object(get_adapter('gpt'), 'refresh_token', side_effect=TimeoutError('temporary refresh network failure')) as refresh:
            selected = Pool(self.pool).get_valid_account('gpt', get_adapter('gpt'))
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(selected['id'], rows[1]['id'])
        self.assertEqual(self.pool.get_account(rows[0]['id'])['status'], 'active')

    def test_scanner_preserves_health_on_refresh_timeout_and_verifies_auth_before_recovery(self):
        row = self.accounts()[0]
        adapter = get_adapter('gpt')
        self.pool.update_account(row['id'], token_exp=1)
        with mock.patch.object(adapter, 'refresh_token', side_effect=TimeoutError('temporary refresh failure')):
            scheduler._check_one(self.pool, adapter, self.pool.get_account(row['id']), 600, .05)
        self.assertEqual(self.pool.get_account(row['id'])['status'], 'active')
        self.pool.update_account(row['id'], status='cooldown', token_exp=int(time.time()) + 3600)
        with mock.patch.object(adapter, 'account_usage', side_effect=UpstreamError(403, 'account disabled')):
            scheduler._check_one(self.pool, adapter, self.pool.get_account(row['id']), 600, .05)
        self.assertEqual(self.pool.get_account(row['id'])['status'], 'cooldown')
        with mock.patch.object(adapter, 'account_usage', return_value={'fetched_at': int(time.time())}) as health:
            scheduler._check_one(self.pool, adapter, self.pool.get_account(row['id']), 600, .05)
        self.assertEqual(health.call_count, 1)
        self.assertEqual(self.pool.get_account(row['id'])['status'], 'active')
        self.assertEqual(self.pool.get_account(row['id'])['last_error'], '')

    def test_http_protocols_share_capacity_queue_and_release_slots(self):
        row = self.accounts()[0]
        self.pool.update_account(row['id'], concurrency=1)
        opened, queued, release = threading.Event(), threading.Event(), threading.Event()
        wait = R._CAPACITY_CHANGED.wait
        def observed_wait(timeout):
            queued.set()
            return wait(timeout)
        class Blocking(io.BytesIO):
            def read1(self, size):
                opened.set()
                if not release.wait(3):
                    raise RuntimeError('fixture was not released')
                return super().read1(size)
        try:
            with mock.patch('core.account_runtime.urlopen', side_effect=[Blocking(F.wire()), io.BytesIO(F.wire())]) as upstream, mock.patch.object(R._CAPACITY_CHANGED, 'wait', side_effect=observed_wait), ThreadPoolExecutor(max_workers=2) as workers:
                one = workers.submit(self.request, '/v1/chat/completions')
                self.assertTrue(opened.wait(2))
                two = workers.submit(self.request, '/v1/responses')
                self.assertTrue(queued.wait(2))
                self.assertEqual(upstream.call_count, 1)
                self.assertEqual(R.capacity(row['id']), 1)
                release.set()
                self.assertIn('pong', one.result(timeout=3).text)
                self.assertIn('pong', two.result(timeout=3).text)
        finally:
            release.set()
        self.assertEqual(R.capacity(row['id']), 0)
        self.assertNotIn(row['id'], R._WAITING)

    def test_full_account_queue_has_bounded_wait_and_never_penalizes_account(self):
        row = self.accounts()[0]
        self.pool.update_account(row['id'], concurrency=1)
        row = self.pool.get_account(row['id'])
        with R.dispatch(self.pool, row, record=False), mock.patch.object(F.config, 'GPT_ACCOUNT_WAIT_TIMEOUT', .02), mock.patch('core.account_runtime.urlopen') as upstream:
            for path in ('/v1/responses', '/v1/chat/completions'):
                response = self.request(path)
                self.assertEqual(response.status_code, 429, response.text)
                self.assertEqual(self.pool.get_account(row['id'])['status'], 'active')
            upstream.assert_not_called()
        self.assertEqual(R.capacity(row['id']), 0)
        self.assertNotIn(row['id'], R._WAITING)

    def test_websocket_queue_timeout_keeps_session_usable_and_does_not_open_upstream(self):
        from tests.test_gpt_import import complete_response
        row = self.accounts()[0]
        self.pool.update_account(row['id'], concurrency=1)
        G.edit_meta(self.pool, row['id'], lambda meta: meta.update(gpt_settings={'ws_mode': 'ctx_pool'}))
        row = self.pool.get_account(row['id'])
        source = SimpleNamespace(settimeout=mock.Mock(), send=mock.Mock(), close=mock.Mock(),
            recv_data=mock.Mock(return_value=(1, json.dumps({'type': 'response.completed', 'response': complete_response()}))))
        with mock.patch.object(F.config, 'GPT_ACCOUNT_WAIT_TIMEOUT', .02), mock.patch('core.gpt_native.open_ws', return_value=source) as upstream:
            with self.client.websocket_connect('/v1/responses', headers={'Authorization': 'Bearer ' + F.config.API_KEY}) as socket:
                with R.dispatch(self.pool, row, record=False):
                    socket.send_json({'type': 'response.create', 'model': 'gpt-5.6-luna', 'input': 'hi'})
                    error = socket.receive_json()
                    self.assertEqual((error['type'], error['error']['code']), ('error', '429'))
                    upstream.assert_not_called()
                socket.send_json({'type': 'response.create', 'model': 'gpt-5.6-luna', 'input': 'hi'})
                self.assertEqual(socket.receive_json()['type'], 'response.completed')
        self.assertEqual(upstream.call_count, 1)
        source.close.assert_called_once()
        self.assertEqual(R.capacity(row['id']), 0)
        self.assertEqual(self.pool.get_account(row['id'])['status'], 'active')
