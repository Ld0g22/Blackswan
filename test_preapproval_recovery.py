"""Pre-approval recovery test: no working quote required for legitimate approval."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from live_executor import LiveExecutor,ExecutionStore
from swap_adapters import NATIVE

class TestPrepareApprovalWithoutQuotes(unittest.TestCase):
    def test_verified_venue_can_prepare_approval_while_quoter_reverts(self):
        with tempfile.TemporaryDirectory() as root:
            store=ExecutionStore(Path(root)/'execution.sqlite3')
            token='0x'+'a'*40
            venue=dict(id='0x'+'d'*64,source='uniswapV4',token0=NATIVE,
                       token1=token,fee=10000,spacing=200,hooks=NATIVE)
            store.setVenue(token,venue,True)
            store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                             ('selected',token,0,'{}','BOUGHT',None))
            store.db.execute("""INSERT INTO positions
                (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
                VALUES(?,?,?,?,?,?,?)""",('selected',token,'100','123',0,0,0))
            calls=[]
            class Swaps:
                async def directQuotes(self,*args,**kwargs):
                    raise AssertionError('Must not depend on liquidity to prepare approval')
                async def approvalNeeded(self,quote,tokenArg,wallet,amount):
                    calls.append(('allowance',quote['venue']['source'],amount))
                    return ('ERC20',tokenArg,'0x'+'d'*40,100)
                def buildApproval(self,approval):
                    return dict(to=approval[1],value=0,data='0x1234')
            class Tx:
                account=SimpleNamespace(address='0x'+'b'*40)
                async def create(self,intent,selection,kind,transaction):
                    calls.append(('create',kind))
                    return dict(intentId=intent)
                async def broadcast(self,intent):
                    calls.append(('broadcast',intent))
                    return 'SENT'
            engine=object.__new__(LiveExecutor)
            engine.store=store; engine.swaps=Swaps(); engine.tx=Tx()
            position=store.db.execute('SELECT * FROM positions').fetchone()
            with patch('live_executor.time.time',return_value=500):
                asyncio.run(engine.preApprove(position))
            self.assertEqual(calls[0],('allowance','uniswapV4',100))
            self.assertEqual(calls[1][0],'create')
            self.assertEqual(calls[2][0],'broadcast')
            store.db.close()

if __name__=='__main__':
    unittest.main()
