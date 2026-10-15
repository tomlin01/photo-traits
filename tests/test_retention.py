"""Transient lifecycle/security regressions. Every data file is disposable synthetic input."""
import copy
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from tests import test_workflow as fixture

w, t = fixture.w, fixture.t


class RetentionTests(unittest.TestCase):
    setUp = fixture.WorkflowTests.setUp
    call = fixture.WorkflowTests.call
    prepare = fixture.WorkflowTests.prepare
    analyze = fixture.WorkflowTests.analyze
    finish = fixture.WorkflowTests.finish
    live_fixture = fixture.WorkflowTests.live_fixture

    def complete(self):
        self.analyze(self.prepare(count=1))
        self.finish()

    def clear(self):
        return self.call('clear_session')['structuredContent']

    def test_creation_ttl_is_fixed_across_plan_changes_and_reads(self):
        deadline = self.s.expires
        self.prepare(count=1)
        self.now += 100
        self.s.edit('Changed synthetic purpose', 'Fixture', fixture.fields()[:1], self.s.revision)
        self.call('session_status')
        self.assertEqual(self.s.expires, deadline)
        self.now = deadline - 0.01
        self.engine.cleanup()
        self.assertTrue(self.s.path.exists())
        self.now = deadline
        self.engine.cleanup()
        self.assertFalse(self.s.path.exists())
        self.assertEqual(self.s.results, [])
        self.assertEqual(self.s.fields, [])

    def test_first_export_shortens_repeated_export_never_extends(self):
        self.complete()
        self.now += 20
        first = self.call('export_csv')
        deadline = self.now + w.EXPORT_TTL
        self.assertEqual(self.s.expires, deadline)
        self.assertEqual(first['structuredContent']['status'], 'generated')
        self.assertFalse(first['structuredContent']['user_save_confirmed'])
        self.now += 140
        second = self.call('export_csv')
        self.assertEqual(self.s.expires, deadline)
        self.assertEqual(first['content'][1], second['content'][1])
        events = list(self.s.path.glob('*-export-generated.json'))
        self.assertEqual(len(events), 2)
        self.assertFalse(list(self.s.path.glob('*-export-receipt.json')))
        self.assertTrue(all(json.loads(e.read_text())['status'] == 'generated' for e in events))
        self.assertFalse(list(self.s.path.rglob('*.csv')))
        self.now = deadline
        with self.assertRaises(w.p.Invalid):
            self.call('export_csv')
        self.assertFalse(self.s.path.exists())

    def test_late_first_export_does_not_extend_overall_lifetime(self):
        self.complete()
        deadline = self.s.expires
        self.now = deadline - 10
        self.call('export_csv')
        self.assertEqual(self.s.expires, deadline)

    def test_failed_export_without_results_does_not_start_short_window(self):
        before = self.s.expires
        with self.assertRaises(w.p.Invalid):
            self.call('export_csv')
        self.assertIsNone(self.s.first_export)
        self.assertEqual(self.s.expires, before)

    def test_export_receipt_io_failure_cannot_extend_generated_window(self):
        self.complete()
        with patch.object(self.s, 'event', side_effect=OSError('Synthetic full disk')):
            with self.assertRaises(OSError):
                self.call('export_csv')
        deadline = self.s.expires
        self.now += 50
        self.call('export_csv')
        self.assertEqual(self.s.expires, deadline)

    def test_clear_wipes_session_but_preserves_returned_candidate_and_human_csv(self):
        self.live_fixture()
        self.complete()
        self.call('review_values', {'plan_sha256': self.s.digest(), 'values': {'surface_mark': 'absent'}})
        result = self.call('export_csv')
        snapshot = self.store.snapshot()
        ledger_bytes = self.store.path.read_bytes()
        cleared = self.clear()
        self.assertEqual(cleared['status'], 'cleared')
        self.assertTrue(cleared['server_files_removed'])
        self.assertFalse(cleared['cleanup_pending'])
        self.assertNotIn(self.s.id, self.engine.sessions)
        self.assertFalse(self.s.path.exists())
        self.assertFalse(self.s.reviews or self.s.results or self.s.photos or self.s.uploaded or self.s.fields)
        self.assertIsNone(self.s.offer)
        rows = list(csv.DictReader(io.StringIO(result['content'][1]['resource']['text'].lstrip('\ufeff'))))
        self.assertEqual(rows[0]['model_candidate'], 'present')
        self.assertEqual(rows[0]['human_value'], 'absent')
        self.assertEqual(rows[0]['evidence_kind'], 'SYNTHETIC_NOT_MODEL_OUTPUT')
        self.assertEqual(self.store.snapshot(), snapshot)
        self.assertEqual(self.store.path.read_bytes(), ledger_bytes)
        self.engine.load_key.assert_not_called()
        self.engine.post_decisions.assert_not_called()

    def test_clear_requires_principal_scopes_owner_and_exact_arguments(self):
        self.prepare()
        before = copy.deepcopy(self.s.state())
        args = {'session_id': self.s.id}
        for principal in (None, w.Principal('other', frozenset(w.SCOPES)),
                          w.Principal(self.principal.subject, frozenset({'results:read'}))):
            with self.subTest(principal=principal), self.assertRaises(w.p.Invalid):
                w.call_tool(self.engine, principal, 'clear_session', args)
        for extra in ({'owner': self.principal.subject}, {'path': str(self.s.path)}, {'force': True}):
            with self.assertRaises(w.p.Invalid):
                self.call('clear_session', extra)
        self.assertEqual(self.s.state(), before)
        self.assertTrue(self.s.path.exists())
        definition = next(d for d in w.tool_definitions() if d['name'] == 'clear_session')
        self.assertFalse(definition['annotations']['readOnlyHint'])
        self.assertTrue(definition['annotations']['destructiveHint'])

    def test_clear_waits_for_inflight_worker_and_preserves_uncertain_budget_hold(self):
        self.live_fixture()
        entered, release = threading.Event(), threading.Event()
        def blocked(_):
            entered.set()
            release.wait(5)
            raise TimeoutError('Synthetic uncertain transport')
        self.engine.synthetic_transport.side_effect = blocked
        self.analyze(self.prepare(count=1))
        try:
            self.assertTrue(entered.wait(2))
            for _ in range(2):
                pending = self.clear()
                self.assertEqual(pending['status'], 'clear_pending')
                self.assertFalse(pending['server_files_removed'])
                self.assertEqual(pending['reason'], 'active_work')
                self.assertTrue(self.s.path.exists())
            self.assertTrue(self.call('session_status')['structuredContent']['cleanup_pending'])
            with self.assertRaises(w.p.Invalid):
                self.call('export_csv')
            self.assertEqual(self.store.snapshot()['pending_reserved_usd'], '0.25')
        finally:
            release.set()
            self.finish()
        self.assertFalse(self.s.path.exists())  # Worker completion triggers cleanup without a sweep.
        self.assertEqual(self.store.snapshot()['pending_reserved_usd'], '0.25')
        self.engine.synthetic_transport.assert_called_once()

    def test_expiry_waits_for_active_worker_and_rejects_new_access(self):
        self.live_fixture()
        entered, release = threading.Event(), threading.Event()
        def blocked(body):
            entered.set(); release.wait(5)
            return fixture.response({'questions': body['questions']})
        self.engine.synthetic_transport.side_effect = blocked
        self.analyze(self.prepare(count=1))
        try:
            self.assertTrue(entered.wait(2))
            self.s.expires = self.engine.clock()
            self.engine.cleanup()
            self.assertTrue(self.s.clear_requested)
            self.assertTrue(self.s.path.exists())
            with self.assertRaises(w.p.Invalid):
                self.call('preview_plan')
        finally:
            release.set(); self.finish()
        self.assertFalse(self.s.path.exists())

    def test_clear_waits_for_download_and_never_stages_afterward(self):
        self.s.edit('Fixture', 'Fixture', fixture.fields()[:1], 0)
        entered, release = threading.Event(), threading.Event()
        errors = []
        def blocked(*_):
            entered.set(); release.wait(5)
            return fixture.mock_files.fixture_jpeg()
        def attach():
            try:
                self.s.attach(self.file, self.s.revision)
            except w.p.Invalid as error:
                errors.append(error)
        self.reader.download.side_effect = blocked
        worker = threading.Thread(target=attach)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.clear()['cleanup_pending'])
            self.assertTrue(self.s.path.exists())
        finally:
            release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.s.path.exists())
        self.assertFalse(self.s.photos)

    def test_expiry_during_download_never_writes_photo(self):
        self.s.edit('Fixture', 'Fixture', fixture.fields()[:1], 0)
        def expired(*_):
            self.now = self.s.expires
            return fixture.mock_files.fixture_jpeg()
        self.reader.download.side_effect = expired
        with self.assertRaises(w.p.Invalid):
            self.s.attach(self.file, self.s.revision)
        self.assertFalse(self.s.path.exists())

    def test_same_root_two_engines_preserve_active_leases(self):
        other = w.WorkflowEngine(self.engine.root)
        other_session = other.new(self.principal)
        self.addCleanup(other.close)
        self.engine.cleanup(); other.cleanup()
        self.assertTrue(self.s.path.exists())
        self.assertTrue(other_session.path.exists())
        self.clear()
        self.assertTrue(other_session.path.exists())

    def child_session(self):
        script = '''
import importlib.util, os, sys, uuid
spec = importlib.util.spec_from_file_location('transient', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
store = module.SessionRoot(sys.argv[2])
sid = uuid.uuid4().hex
path, lease = store.create(sid)
fd = os.open(path / (uuid.uuid4().hex + '-plan.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, b'{"synthetic": true}'); os.close(fd)
print(sid, flush=True)
input()
os._exit(0)
'''
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', script,
            str(w.p.PACKAGE / 'transient.py'), str(self.engine.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stop():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(stop)
        sid = child.stdout.readline().strip()
        self.assertRegex(sid, r'^[a-f0-9]{32}$')
        return child, self.engine.root / sid

    def test_startup_reaps_crashed_process_but_not_running_process(self):
        child, path = self.child_session()
        other = w.WorkflowEngine(self.engine.root)
        self.addCleanup(other.close)
        self.assertTrue(path.exists())
        child.communicate(input='exit\n', timeout=5)
        self.assertEqual(child.returncode, 0)
        restarted = w.WorkflowEngine(self.engine.root)
        self.addCleanup(restarted.close)
        self.assertFalse(path.exists())
        self.assertTrue(self.s.path.exists())

    def test_periodic_sweep_reaps_orphan_created_after_startup(self):
        child, path = self.child_session()
        child.communicate(input='exit\n', timeout=5)
        self.now += w.p.SWEEP_INTERVAL - 1
        self.engine.periodic_cleanup()
        self.assertTrue(path.exists())
        self.now += 1
        self.engine.periodic_cleanup()
        self.assertFalse(path.exists())
        self.assertTrue(self.s.path.exists())

    def test_unmarked_root_even_empty_is_never_adopted_or_swept(self):
        old = self.root / 'data/derived/legacy'
        old.mkdir()
        for with_data in (False, True):
            if with_data:
                (old / 'old-real-data-sentinel.json').write_text('synthetic sentinel')
            with self.assertRaises((OSError, w.p.transient.UnsafeStorage)):
                w.WorkflowEngine(old)
            self.assertFalse((old / w.p.transient.ROOT_MARKER).exists())
        self.assertEqual((old / 'old-real-data-sentinel.json').read_text(), 'synthetic sentinel')

    def test_unmarked_or_mismarked_session_and_unrelated_directories_survive(self):
        unmarked = self.engine.root / uuid.uuid4().hex
        unmarked.mkdir(mode=0o700)
        (unmarked / 'sentinel').write_text('keep')
        unrelated = self.engine.root / 'unrelated'
        unrelated.mkdir(); (unrelated / 'sentinel').write_text('keep')
        sid = uuid.uuid4().hex
        path, lease = self.engine.storage.create(sid)
        lease.close()
        (path / w.p.transient.LEASE).write_bytes(b'wrong owner')
        self.engine.cleanup()
        self.assertTrue(unmarked.exists() and unrelated.exists() and path.exists())

    def test_traversal_and_root_symlinks_are_rejected(self):
        alias = self.root / 'data/derived/alias'
        alias.symlink_to(self.engine.root, target_is_directory=True)
        for root in (alias, alias / 'nested', self.engine.root / '../escape'):
            with self.assertRaises((w.p.Invalid, t.SafeError, OSError, w.p.transient.UnsafeStorage)):
                w.WorkflowEngine(root)
        for sid in ('../other', str(self.s.path), '../../budget.sqlite3', ''):
            with self.assertRaises(w.p.Invalid):
                self.call('clear_session', {'session_id': sid})
            with self.assertRaises(w.p.transient.UnsafeStorage):
                self.engine.storage.remove(sid, self.s.lease)
        self.assertTrue(self.s.path.exists())

    def test_replaced_root_is_not_followed(self):
        original = self.engine.root
        moved = original.with_name('moved-owned-root')
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        self.engine.cleanup()
        self.assertTrue((moved / self.s.id).exists())
        result = self.clear()
        self.assertFalse(result['server_files_removed'])
        original.unlink(); moved.rename(original)
        self.engine.cleanup()
        self.assertFalse(self.s.path.exists())

    def test_session_symlink_is_never_followed(self):
        path = self.s.path
        moved = path.with_name('unrelated-session-copy')
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        result = self.clear()
        self.assertTrue(result['cleanup_pending'])
        self.assertTrue((moved / w.p.transient.LEASE).exists())
        path.unlink(); moved.rename(path)
        self.engine.cleanup()
        self.assertFalse(path.exists())

    def test_nested_symlink_hardlink_and_unknown_files_block_deletion(self):
        self.complete()
        outside = self.root / 'outside-synthetic.json'
        outside.write_text('do not delete')
        outside.chmod(0o600)
        for kind in ('file_symlink', 'directory_symlink', 'hardlink', 'budget.sqlite3', 'unrelated'):
            with self.subTest(kind=kind):
                if kind == 'file_symlink':
                    unsafe = self.s.path / (uuid.uuid4().hex + '-plan.json')
                    unsafe.symlink_to(outside)
                elif kind == 'directory_symlink':
                    unsafe = self.s.path / uuid.uuid4().hex
                    unsafe.symlink_to(self.root, target_is_directory=True)
                elif kind == 'hardlink':
                    unsafe = self.s.path / (uuid.uuid4().hex + '-plan.json')
                    os.link(outside, unsafe)
                else:
                    unsafe = self.s.path / kind
                    unsafe.write_text('unrelated synthetic file')
                before = set(self.s.path.iterdir())
                self.assertTrue(self.clear()['cleanup_pending'])
                self.assertEqual(set(self.s.path.iterdir()), before)
                self.assertEqual(outside.read_text(), 'do not delete')
                unsafe.unlink()
        self.engine.cleanup()
        self.assertFalse(self.s.path.exists())

    def test_budget_store_inside_disposable_root_is_refused_before_sweep(self):
        with self.assertRaises(w.p.Invalid):
            w.WorkflowEngine(self.engine.root,
                budget_store=SimpleNamespace(path=self.s.path / 'budget.sqlite3'))
        self.assertTrue(self.s.path.exists())

    def test_photo_only_purge_refuses_redirected_session_and_photo_links(self):
        self.prepare(count=1)
        handle = self.s.photos[0]['handle']
        original = self.s.path
        moved = original.with_name('saved-synthetic-session')
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaises((OSError, w.p.transient.UnsafeStorage)):
            self.engine.storage.purge_photos(self.s.id, self.s.lease, [handle])
        self.assertTrue((moved / (handle + '.jpg')).exists())
        original.unlink(); moved.rename(original)
        photo = original / (handle + '.jpg')
        outside = self.root / 'outside-synthetic.jpg'
        photo.rename(outside)
        photo.symlink_to(outside)
        with self.assertRaises(w.p.transient.UnsafeStorage):
            self.engine.storage.purge_photos(self.s.id, self.s.lease, [handle])
        self.assertTrue(outside.exists())
        photo.unlink()
        self.assertTrue(self.clear()['server_files_removed'])

    def test_legacy_session_has_same_ttl_export_window_and_clear(self):
        engine = w.p.Engine(self.root / 'data/derived/legacy-new-fixture', clock=lambda: self.now)
        self.addCleanup(engine.close)
        session = engine.new()
        session.edit('Fixture', 'Fixture', fixture.fields()[:1], 0)
        session.upload(fixture.mock_files.fixture_jpeg(), 'image/jpeg', session.revision)
        session.confirm(session.digest(), list(session.uploaded), False)
        session.execute(session.digest()); session.thread.join(3)
        self.assertFalse(session.thread.is_alive())
        session.csv_bytes()
        self.assertEqual(session.expires, self.now + w.EXPORT_TTL)
        self.assertTrue(engine.clear(session)['server_files_removed'])
        fresh = engine.new()
        self.now += w.SESSION_TTL
        engine.periodic_cleanup()
        self.assertFalse(fresh.path.exists())


if __name__ == '__main__':
    unittest.main()
