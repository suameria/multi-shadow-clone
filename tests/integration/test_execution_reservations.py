import tempfile
from pathlib import Path
import unittest
from kagebunshin.execution.domain.admission import Binding, Grant, Rejected
from kagebunshin.execution.application.reservations import Reservations
from kagebunshin.execution.infrastructure.sqlite_store import SQLiteOperationStore


class ReservationTest(unittest.TestCase):
    def test_check_contract_is_required_at_start_and_receipt_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'operations.sqlite3'
            app = Reservations(SQLiteOperationStore(path),lambda:10)
            binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
            grant = Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'syntax'}),1,100,10000)
            with self.assertRaises(Rejected):
                app.register(grant,check_contracts={'other':'a'*64})
            app.register(grant,check_contracts={'syntax':'a'*64})
            app.reserve(binding,[{'call_id':'one','operation':'run_check','arguments':{'check_id':'syntax'}}])
            for contract in (None,'b'*64):
                with self.subTest(contract=contract), self.assertRaises(Rejected):
                    app.begin(binding,'one',check_contract=contract)
            started = app.begin(binding,'one',check_contract='a'*64)
            with self.assertRaises(Rejected):
                app.finish(binding,'one',started['payload_hash'],{'operation':'run_check','check':{'definition_hash':'b'*64}})
            self.assertEqual(app.store.read('g')['calls']['one']['state'],'unknown')
            receipt={'operation':'run_check','check':{'definition_hash':'a'*64,'passed':False}}
            result=app.finish(binding,'one',started['payload_hash'],receipt)
            self.assertEqual(result['state'],'completed')
            self.assertFalse(result['receipt']['check']['passed'])

    def test_atomic_rejection_restart_replay_and_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'operations.sqlite3'
            store = SQLiteOperationStore(path)
            app = Reservations(store, lambda: 10)
            binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
            app.register(Grant(binding, frozenset({'run_check'}), frozenset(), frozenset({'syntax'}),20,100,10000))
            def request(i):
                return {'call_id': str(i),'operation':'run_check','arguments':{'check_id':'syntax'}}
            before = store.read('g')
            with self.assertRaises(Rejected):
                app.reserve(binding,[request(i) for i in range(900)])
            self.assertEqual(store.read('g'), before)
            accepted = app.reserve(binding,[request(1),request(2)])
            self.assertEqual(accepted['grant']['remaining'],18)
            restarted = Reservations(SQLiteOperationStore(path),lambda:10)
            self.assertEqual(restarted.reserve(binding,[request(1),request(2)]),accepted)
            with self.assertRaises(Rejected):
                restarted.reserve(binding,[request(3),dict(request(4),operation='shell')])
            self.assertEqual(store.read('g'),accepted)
            stopped = restarted.stop('g')
            self.assertEqual(stopped['grant']['binding']['stop_epoch'],1)
            self.assertEqual(stopped['grant']['remaining'],20)
            self.assertEqual({c['state'] for c in stopped['calls'].values()},{'cancelled'})
            self.assertEqual(restarted.stop('g'),stopped)
            with self.assertRaises(Rejected):
                restarted.reserve(binding,[request(3)])
            self.assertEqual(store.read('g'),stopped)

    def test_started_operation_survives_restart_and_late_result_is_quarantined(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'operations.sqlite3'
            app = Reservations(SQLiteOperationStore(path),lambda:10)
            binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
            app.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'syntax'}),2,100,10000))
            request = {'call_id':'one','operation':'run_check','arguments':{'check_id':'syntax'}}
            app.reserve(binding,[request])
            started = app.begin(binding,'one')
            self.assertEqual(started['state'],'unknown')
            restarted = Reservations(SQLiteOperationStore(path),lambda:10)
            with self.assertRaises(Rejected):
                restarted.begin(binding,'one')
            restarted.stop('g')
            result = restarted.finish(binding,'one',started['payload_hash'],{'exit_code':0})
            self.assertEqual(result['state'],'quarantined')
            self.assertEqual(restarted.finish(binding,'one',started['payload_hash'],{'exit_code':0}),result)
            with self.assertRaises(Rejected):
                restarted.finish(binding,'one',started['payload_hash'],{'exit_code':1})
            self.assertEqual(restarted.store.read('g')['grant']['remaining'],1)

    def test_concurrent_last_slot_and_begin_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'operations.sqlite3'
            first = Reservations(SQLiteOperationStore(path),lambda:10)
            second = Reservations(SQLiteOperationStore(path),lambda:10)
            binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
            first.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'syntax'}),1,100,10000))
            gate = Barrier(2)
            def reserve(pair):
                app, ident = pair
                gate.wait(timeout=5)
                try:
                    app.reserve(binding,[{'call_id':ident,'operation':'run_check','arguments':{'check_id':'syntax'}}])
                    return ident
                except Rejected:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(reserve,[(first,'one'),(second,'two')]))
            winners = [r for r in results if r is not None]
            self.assertEqual(len(winners),1)
            self.assertEqual(first.store.read('g')['grant']['remaining'],0)
            gate = Barrier(2)
            def begin(app):
                gate.wait(timeout=5)
                try:
                    app.begin(binding,winners[0])
                    return True
                except Rejected:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(begin,[first,second]))
            self.assertEqual(sum(results),1)
            self.assertEqual(first.store.read('g')['calls'][winners[0]]['state'],'unknown')

    def test_begin_revalidates_saved_capability_before_any_effect(self):
        import json
        from hashlib import sha256
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOperationStore(Path(folder)/'operations.sqlite3')
            app = Reservations(store, lambda:10)
            binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
            app.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'syntax'}),1,100,10000))
            app.reserve(binding,[{'call_id':'one','operation':'run_check','arguments':{'check_id':'syntax'}}])
            # A storage writer has a valid outer checksum but changes the command.
            def alter(current):
                payload = json.dumps({'operation':'run_check','arguments':{'check_id':'unregistered'}},sort_keys=True,separators=(',',':'))
                current['calls']['one'].update(payload_json=payload,payload_hash=sha256(payload.encode()).hexdigest())
                return current
            store.transact('g',alter)
            with self.assertRaises(Rejected): app.begin(binding,'one')
            self.assertEqual(store.read('g')['calls']['one']['state'],'reserved')
            self.assertEqual(store.read('g')['grant']['remaining'],0)

    def test_stop_racing_begin_never_releases_a_started_reservation(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'operations.sqlite3'
            first = Reservations(SQLiteOperationStore(path),lambda:10)
            second = Reservations(SQLiteOperationStore(path),lambda:10)
            binding = Binding('g','r','n','a','t','turn',0,'w','c'*64)
            first.register(Grant(binding,frozenset({'run_check'}),frozenset(),frozenset({'syntax'}),2,100,10000))
            first.reserve(binding,[{'call_id':i,'operation':'run_check','arguments':{'check_id':'syntax'}} for i in ('race','pending')])
            gate = Barrier(2)
            def begin():
                gate.wait(timeout=5)
                try:
                    return first.begin(binding,'race')
                except Rejected:
                    return None
            def stop():
                gate.wait(timeout=5)
                return second.stop('g')
            with ThreadPoolExecutor(max_workers=2) as pool:
                start_future = pool.submit(begin)
                stop_future = pool.submit(stop)
                started, stopped = start_future.result(), stop_future.result()
            final = SQLiteOperationStore(path).read('g')
            self.assertEqual(final,stopped)
            self.assertEqual(final['calls']['pending']['state'],'cancelled')
            if started is None:
                self.assertEqual(final['calls']['race']['state'],'cancelled')
                self.assertEqual(final['grant']['remaining'],2)
            else:
                self.assertEqual(final['calls']['race']['state'],'unknown')
                self.assertEqual(final['grant']['remaining'],1)
                result = first.finish(binding,'race',started['payload_hash'],{'exit_code':0})
                self.assertEqual(result['state'],'quarantined')
            before = first.store.read('g')
            self.assertEqual(second.stop('g'),before)
            with self.assertRaises(Rejected):
                first.begin(binding,'pending')
