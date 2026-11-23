"""Launcher integration tests without wallet decryption, RPC or broadcasts.

The test drives the real CLI/parser/manifest/child-argv paths, intercepting
all actual subprocess starts. No live transactions can be sent.
"""
from contextlib import ExitStack,redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import io
import json
import sys
import tempfile
import unittest

import run_live_campaign as launch
from campaign_config import FLEX_KIND, FLEX_PREFIX
from live_executor import ExecutionStore

WALLET='0x'+'1'*40


class FakeChild:
    def poll(self):return None


class FlexLauncherIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.env=self.base/'.env'
        self.env.write_text('NOT_A_REAL_RPC_SETTING\n')
        self.walletFile=self.base/'wallet.json'
        self.walletFile.write_text('{}')
        self.data=self.base/'data'
        self.data.mkdir()
        self.childCmd=None
        self.started=[]
        self.realIsFile=Path.is_file

    def fakeIsFile(self,path):
        if (path.name=='python' and path.parent.name=='bin'
                and path.parent.parent.name=='.venv'):
            return True
        return self.realIsFile(path)

    def fakeStartWorker(self,label,cmd,messages):
        self.started.append(label)
        if label=='SELECTOR':
            dbPath=Path(cmd[cmd.index('--db')+1])
            dbPath.touch()
        return FakeChild()

    def abortBeforeExecutor(self,cmd,**kwargs):
        self.childCmd=cmd
        raise RuntimeError('STOP_BEFORE_REAL_EXECUTOR')

    def patched(self,args):
        stack=ExitStack()
        stack.enter_context(patch.object(sys,'argv',['run_live_campaign.py',*args]))
        stack.enter_context(patch.object(Path,'is_file',lambda path: self.fakeIsFile(path)))
        stack.enter_context(patch.object(launch,'validateWalletFile',return_value=WALLET))
        stack.enter_context(patch.object(launch,'otherLiveExecutors',return_value=[]))
        stack.enter_context(patch.object(launch,'detectorReady',return_value=True))
        stack.enter_context(patch.object(launch,'startWorker',side_effect=self.fakeStartWorker))
        stack.enter_context(patch.object(launch,'stopWorkers'))
        stack.enter_context(patch.object(launch,'drain'))
        stack.enter_context(patch.object(launch.getpass,'getpass',return_value='FAKE_ONLY'))
        stack.enter_context(patch('subprocess.run',return_value=SimpleNamespace(returncode=0)))
        stack.enter_context(patch('subprocess.Popen',side_effect=self.abortBeforeExecutor))
        stack.enter_context(patch.object(launch,'newExperimentId',return_value='CI_TEST'))
        return stack

    def flags(self):
        return ['--flex-lamb','--env',str(self.env),
                '--wallet-file',str(self.walletFile)]

    def test_start_creates_immutable_three_by_twenty_and_passes_child_args(self):
        args=['start',*self.flags(),'--buy-gbp','0.2',
              '--max-purchases','3','--data-dir',str(self.data),'--arm-live']
        with self.patched(args),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'STOP_BEFORE_REAL_EXECUTOR'):
                launch.main()
        run=self.data/(FLEX_PREFIX+'CI_TEST')
        manifest=json.loads((run/'campaign.json').read_text())
        self.assertEqual(manifest['campaignKind'],FLEX_KIND)
        self.assertEqual(manifest['gbpEach'],'0.20')
        self.assertEqual(manifest['targetPurchases'],3)
        self.assertEqual(manifest['probeGbp'],'0.01')
        self.assertEqual(manifest['survivalBlocks'],500)
        self.assertEqual(manifest['selectorDecisionSeconds'],20)
        self.assertEqual(manifest['selectorMaxBuyingBlocks'],10)
        self.assertEqual(manifest['sellTranches'],
                         {'2x':80,'50x':10,'100x':5,'manual':5})
        self.assertIn('--flex-lamb',self.childCmd)
        self.assertEqual(self.childCmd[self.childCmd.index('--buy-gbp')+1],'0.20')
        self.assertEqual(self.childCmd[self.childCmd.index('--max-purchases')+1],'3')
        self.assertEqual(self.childCmd[self.childCmd.index('--campaign-wallet')+1],WALLET)
        self.assertEqual(self.started,['DETECTOR','SELECTOR'])

    def test_resume_uses_saved_values_without_stake_or_cap_flags(self):
        run=self.makePreparedRun()
        args=['resume',*self.flags(),'--run-dir',str(run),'--arm-live']
        with self.patched(args),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'STOP_BEFORE_REAL_EXECUTOR'):
                launch.main()
        self.assertEqual(self.childCmd[self.childCmd.index('--buy-gbp')+1],'0.50')
        self.assertEqual(self.childCmd[self.childCmd.index('--max-purchases')+1],'5')
        self.assertEqual(self.childCmd[self.childCmd.index('--campaign-wallet')+1],WALLET)

    def makePreparedRun(self):
        run=self.data/(FLEX_PREFIX+'EXISTING')
        run.mkdir()
        manifest=dict(name=run.name,createdUtc='2026-11-11T00:00:00Z',
                      chainId=4663,walletPublicAddress=WALLET,mode='REAL_MONEY',
                      targetPurchases=5,gbpEach='0.50',configVersion=1,
                      entryPolicy='uniswapV4_nonPons_only',
                      quoteEngine='original_peak_tracker_Tracker_best',
                      exitPolicy='latched_2x_50x_100x_manual5',
                      sellTranches={'2x':80,'50x':10,'100x':5,'manual':5},
                      campaignKind=FLEX_KIND,probeGbp='0.01',survivalBlocks=500,
                      selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)
        (run/'campaign.json').write_text(json.dumps(manifest))
        store=ExecutionStore(run/'execution.sqlite3',sacrifice=True,
                             probeWaitBlocks=500,flexLamb=True,flexBuyGbp='0.50',
                             flexMaxPurchases=5,flexWalletAddress=WALLET)
        store.db.close()
        return run

    def test_changed_manifest_is_refused_before_wallet_password_and_workers(self):
        run=self.makePreparedRun()
        manifestPath=run/'campaign.json'
        manifest=json.loads(manifestPath.read_text())
        manifest['targetPurchases']=3
        manifestPath.write_text(json.dumps(manifest))
        args=['resume',*self.flags(),'--run-dir',str(run),'--arm-live']
        with self.patched(args),redirect_stdout(io.StringIO()):
            with patch.object(launch.getpass,'getpass',side_effect=AssertionError('PASSWORD_REQUESTED')):
                with self.assertRaisesRegex(RuntimeError,'FLEX_LEDGER_CONFIG_MISMATCH'):
                    launch.main()
        self.assertEqual(self.started,[])
        self.assertIsNone(self.childCmd)

    def test_status_derives_frozen_settings_from_run_without_wallet(self):
        run=self.makePreparedRun()
        captured=[]
        def fakeExec(binary,argv):
            captured.extend(argv)
            raise RuntimeError('EXEC_INTERCEPTED')
        args=['status','--flex-lamb','--run-dir',str(run)]
        with patch.object(sys,'argv',['run_live_campaign.py',*args]),\
             patch.object(Path,'is_file',lambda path: self.fakeIsFile(path)),\
             patch.object(launch.os,'execv',side_effect=fakeExec):
            with self.assertRaisesRegex(RuntimeError,'EXEC_INTERCEPTED'):
                launch.main()
        self.assertIn('--flex-lamb',captured)
        self.assertEqual(captured[captured.index('--buy-gbp')+1],'0.50')
        self.assertEqual(captured[captured.index('--max-purchases')+1],'5')
        self.assertEqual(captured[captured.index('--campaign-wallet')+1],WALLET)


if __name__=='__main__':
    unittest.main()
