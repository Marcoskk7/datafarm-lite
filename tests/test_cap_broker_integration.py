"""Real DataFarm broker, fake simulator: verify the CaP client cannot bypass gates."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from datafarm.cap.xh import XHEngine


@unittest.skipIf(sys.platform=='win32','The existing expert-harness broker is POSIX-only')
class BrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_expert_harness_broker import BrokerFixture
        self.fixture=BrokerFixture();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine=XHEngine(self.fixture.paths.root,'ToyExpert','primitive.jsonl',timeout_s=1,poll_s=.001)

    def tick(self,_):
        f=self.fixture;f.clock.advance(1);f.broker.tick(force_poll=True)
        for attempt in f.broker.attempts.values():
            target=Path(attempt['run_dir'])/'primitive.jsonl'
            if not target.exists():
                target.write_text('{"event":"primitive","event_id":0,"api":"move","before":[0,0],"after":[1,0],"result":{"ok":true}}\n')

    def test_native_broker_executes_then_accepts_submission_without_exposing_holdout(self):
        from xh_helpers import expert
        with patch('datafarm.cap.xh.time.sleep',self.tick):
            result=self.engine.evaluate(expert(1),[3],'debug')
            self.assertTrue(result['episodes'][0]['success'])
            self.assertEqual(self.fixture.broker.counted(),1)
            submitted=self.engine.submit(expert(1))
            self.assertEqual(submitted['status'],'accepted')
        self.assertEqual(len(self.fixture.state.data['submissions']),1)
        self.assertTrue(self.fixture.state.data['submissions'][0]['final'])
        self.assertNotIn('holdout',result)

    def test_native_broker_refuses_hidden_seed_and_source_audit_violation(self):
        from xh_helpers import HOLDOUT,expert
        with patch('datafarm.cap.xh.time.sleep',self.tick):
            with self.assertRaisesRegex(ValueError,'development seed'):
                self.engine.evaluate(expert(1),[HOLDOUT[0]],'debug')
            with self.assertRaisesRegex(ValueError,'audit'):
                self.engine.evaluate('import os\n'+expert(1),[3],'debug')
        self.assertEqual(len(self.fixture.backend.launched),0)
