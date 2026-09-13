import unittest

from kagebunshin.execution.domain.admission import Rejected
from kagebunshin.execution.domain.container_lifecycle import transition


class ContainerLifecycleTest(unittest.TestCase):
    def test_unknown_effects_cannot_be_retried_or_retired_as_completed(self):
        claimed={'phase':'claimed','owner':'fixture'}
        creating=transition(claimed,'reserve_create')
        self.assertEqual(claimed['phase'],'claimed')
        for event in ('reserve_create','reserve_start','absent','reserve_retire'):
            with self.assertRaises(Rejected): transition(creating,event)
        created=transition(creating,'created',container_id='a'*64)
        with self.assertRaises(Rejected): transition(created,'created',container_id='b'*64)
        starting=transition(created,'reserve_start')
        for event in ('reserve_start','reserve_create','reserve_retire','absent'):
            with self.assertRaises(Rejected): transition(starting,event)
        stopping=transition(starting,'reserve_stop')
        exited=transition(stopping,'exited',result={'exit_code':137,'oom_killed':False})
        self.assertEqual(transition(exited,'exited',result=exited['result']),exited)
        with self.assertRaises(Rejected):
            transition(exited,'exited',result={'exit_code':0,'oom_killed':False})
        retired=transition(transition(exited,'reserve_retire'),'absent')
        self.assertEqual(transition(retired,'absent'),retired)
        self.assertEqual(retired['result']['exit_code'],137)
        with self.assertRaises(Rejected): transition(retired,'reserve_create')

    def test_created_but_never_started_can_be_retired_without_success_receipt(self):
        state={'phase':'created','container_id':'a'*64}
        retired=transition(transition(state,'reserve_retire'),'absent')
        self.assertNotIn('result',retired)
        self.assertEqual(state['phase'],'created')
