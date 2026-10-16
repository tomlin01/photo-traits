"""Synthetic ledger faults, concurrency and process-restart checks; no networking."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import traits as t
from tests.test_plugin import p
from tests.test_traits import response

b = p.spending


def sha(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'ledger.sqlite3'
        self.store = b.SQLiteBudgetStore.provision(self.path,
            opening_spent_upper_usd='0', opening_evidence_sha256=sha('synthetic audit, no actual spend'))

    def reopen(self):
        return b.SQLiteBudgetStore(self.path, ledger_id=self.store.ledger_id)

    def reserve(self, n=1, store=None):
        return (store or self.store).reserve(sha(n), sha('request'), sha('price'), '0.25')

    def settle(self, n=1, store=None, **overrides):
        args = dict(cost_upper_usd='0.00022', input_tokens=1000,
                    response_sha256=sha('response'), price_sha256=sha('price'))
        return (store or self.store).settle(sha(n), **{**args, **overrides})

    def test_opening_balance_is_explicit_and_cannot_reset_existing_ledger(self):
        with self.assertRaises(TypeError):
            b.SQLiteBudgetStore.provision(self.path)
        for amount in ('NaN', '-1', '5.01', 'Infinity', '0.0000000001', True, 0.1):
            with self.subTest(amount=amount), self.assertRaises(b.BudgetError):
                b.SQLiteBudgetStore.provision(self.path.parent / 'invalid.sqlite3',
                    opening_spent_upper_usd=amount, opening_evidence_sha256=sha('audit'))
        self.reserve()
        with self.assertRaises(b.BudgetError):
            b.SQLiteBudgetStore.provision(self.path, opening_spent_upper_usd='0',
                                         opening_evidence_sha256=sha('another authorization'))
        self.assertEqual(self.reopen().snapshot()['committed_upper_usd'], '0.25')

    def test_concurrent_store_instances_atomically_share_one_total(self):
        def attempt(n):
            try:
                self.reserve(n, self.reopen())
                return True
            except b.BudgetError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(attempt, range(32)))
        self.assertEqual(sum(accepted), 20)
        self.assertEqual(self.reopen().snapshot()['committed_upper_usd'], '5')
        self.assertEqual(self.reopen().snapshot()['pending_attempts'], 20)
        with self.assertRaises(b.BudgetError): self.reserve('new-process-new-authorization')

    def test_cross_process_concurrency_and_committed_crash_do_not_reset(self):
        code = '''
import hashlib, importlib.util, os, sys
spec = importlib.util.spec_from_file_location('budget', sys.argv[1])
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
s = b.SQLiteBudgetStore(sys.argv[2], ledger_id=sys.argv[3])
for n in range(12):
    try:
        s.reserve(hashlib.sha256((sys.argv[4]+str(n)).encode()).hexdigest(), 'a'*64, 'b'*64, '0.25')
    except b.BudgetError:
        pass
os._exit(0)
'''
        processes = [subprocess.Popen([sys.executable, '-I', '-c', code, str(Path(b.__file__)),
                     str(self.path), self.store.ledger_id, str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for i in range(3)]
        try:
            for proc in processes:
                out, err = proc.communicate(timeout=15)
                self.assertEqual((proc.returncode, out, err), (0, b'', b''))
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill(); proc.communicate()
        self.assertEqual(self.reopen().snapshot()['attempts'], 20)
        self.assertEqual(self.reopen().snapshot()['available_usd'], '0')

    def test_precommit_crash_rolls_back_and_does_not_authorize_send(self):
        code = '''
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('BEGIN IMMEDIATE')
db.execute('INSERT INTO attempts (attempt_id, request_sha256, price_sha256, reserved_nanos) VALUES (?, ?, ?, ?)', ('a'*64, 'b'*64, 'c'*64, 250000000))
os._exit(0)
'''
        subprocess.run([sys.executable, '-I', '-c', code, str(self.path)], check=True, timeout=10,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(self.reopen().snapshot()['attempts'], 0)

    def test_usage_settlement_and_replay_are_atomic_idempotent_and_conservative(self):
        self.reserve()
        with self.assertRaises(b.BudgetError): self.reserve()
        one = self.settle()
        self.assertEqual(one['settled_upper_usd'], '0.00022')
        self.assertEqual(one['available_usd'], '4.99978')
        self.assertEqual(self.settle(store=self.reopen()), one)
        with self.assertRaises(b.BudgetError): self.reserve()
        for kwargs in ({'input_tokens': True}, {'input_tokens': 922001}, {'cost_upper_usd': '0'},
                       {'cost_upper_usd': '0.26'}, {'cost_upper_usd': '0.00021'},
                       {'response_sha256': sha('different response')}, {'price_sha256': sha('different price')}):
            with self.subTest(kwargs=kwargs), self.assertRaises(b.BudgetError): self.settle(**kwargs)
        self.assertEqual(self.store.snapshot(), one)

    def test_missing_replaced_corrupt_and_symlink_storage_fail_closed(self):
        other = b.SQLiteBudgetStore(self.path, ledger_id='0'*32)
        with self.assertRaises(b.BudgetError): other.snapshot()
        self.path.unlink()
        with self.assertRaises(b.BudgetError): self.reserve()
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b'PRIVATE_ERROR_CONTENT_NOT_TO_ECHO')
        self.path.chmod(0o600)
        with self.assertRaises(b.BudgetError) as exc: self.reserve()
        self.assertNotIn('PRIVATE_ERROR', str(exc.exception))
        self.path.unlink()
        self.path.symlink_to(self.path.parent / 'missing')
        with self.assertRaises(b.BudgetError): self.reserve()

    def test_held_transaction_refuses_on_timeout_without_spending(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(b.BudgetError): self.reserve()
        finally:
            db.close()
        self.assertEqual(self.reopen().snapshot()['attempts'], 0)

    def test_local_reference_never_claims_cloud_durability(self):
        self.store.ensure_ready(for_live=False)
        with self.assertRaises(b.BudgetError): self.store.ensure_ready(for_live=True)
        for maximum in ('0', '0.1', '0.26', '5', 'NaN'):
            with self.assertRaises(b.BudgetError):
                self.store.reserve(sha(1), sha('request'), sha('price'), maximum)
        self.assertEqual(self.store.snapshot()['attempts'], 0)


class PricingTests(unittest.TestCase):
    def test_unknown_stale_future_and_changed_prices_fail_closed(self):
        for name, value in [('PRICE_DATE', '2000-01-01'), ('PRICE_DATE', None),
                            ('PRICE_DATE', (date.today()+timedelta(days=1)).isoformat()),
                            ('INPUT_USD_PER_MILLION', None), ('UPPER_USD_PER_MILLION', Decimal('NaN')),
                            ('MAX_INPUT_TOKENS', 1_000_000), ('REQUEST_RESERVE_USD', Decimal('.1')),
                            ('MODEL', 'another-model'), ('ENDPOINT', 'https://api.openai.com/v1/responses')]:
            with self.subTest(name=name, value=value), patch.object(t, name, value), self.assertRaises(t.SafeError):
                t.checked_pricing()

    def test_full_model_ceiling_is_covered_without_guessing_image_tokens(self):
        with patch.object(t, 'PRICE_DATE', date.today().isoformat()):
            policy = t.checked_pricing()
            self.assertEqual(Decimal(policy['upper_usd_per_million']) * policy['max_input_tokens'] / 1_000_000,
                             Decimal('0.20284'))
            self.assertGreaterEqual(Decimal(policy['reservation_usd']), Decimal('0.20284'))

    def test_complete_usage_contract_no_cache_discount_or_invented_output_charge(self):
        with patch.object(t, 'PRICE_DATE', date.today().isoformat()):
            raw = response({'questions': []})
            raw['usage']['input_tokens_details']['cached_tokens'] = 900
            self.assertEqual(Decimal(t.usage_cost(raw)['cost_upper_usd']), Decimal('.00022'))
            for key, value in [('input_tokens', True), ('input_tokens', 0), ('input_tokens', -1),
                               ('input_tokens', 922001), ('input_tokens', 1000.0), ('total_tokens', 999),
                               ('output_tokens', 1), ('input_tokens_details', {}), ('output_tokens_details', None)]:
                mutated = json.loads(json.dumps(raw))
                mutated['usage'][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(t.SafeError): t.usage_cost(mutated)
            with self.assertRaises(t.SafeError): t.usage_cost({'usage': raw['usage'], 'model': 'other'})

    def test_legacy_batch_cannot_bypass_project_ledger(self):
        with patch.object(t, 'load_key') as key, patch.object(t, 'post_decisions') as post:
            with self.assertRaises(t.SafeError): t.run_live(Path('/unused'), live=True, approve_upload=True)
            key.assert_not_called(); post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
