"""Carry established Build Two rug bans into configured buying-block campaigns."""
import unittest
from types import SimpleNamespace
from live_executor import LiveExecutor

ZERO='0x'+'0'*40
TOKEN='0x'+'1'*40
BANNED='0x'+'1'*36+'7777'

class BuyingBlockRugBanTests(unittest.TestCase):
    def exclusion(self,name,sacrifice,token=TOKEN,fee=100,spacing=1,hooks=ZERO,flex=False):
        config={'selector':{'name':name}} if name else None
        executor=object.__new__(LiveExecutor)
        executor.store=SimpleNamespace(configuredCampaign=config,sacrifice=sacrifice,flexLamb=flex)
        executor.ponsOrigin=lambda token:False
        return executor.entryExclusion(token,{'event':{'source':'uniswapV4'},
            'venue':{'source':'uniswapV4','fee':fee,'spacing':spacing,'hooks':hooks}})

    def test_established_exclusions_with_or_without_sacrifice(self):
        for name in ('buyingBlocks','concentration'):
            for sacrifice in (True,False):
                with self.subTest(name=name,sacrifice=sacrifice):
                    self.assertEqual(self.exclusion(name,sacrifice,token=BANNED),
                        'EXPERIMENT_BANNED_TOKEN_SUFFIX_7777')
                    self.assertEqual(self.exclusion(name,sacrifice,fee=10000,spacing=200),
                        'BANNED_V4_RUG_TEMPLATE_1PCT_200_ZERO_HOOK')

    def test_no_new_experimental_template_ban(self):
        for fee,spacing,hooks in ((100,1,ZERO),(1,1,ZERO),(10000,1,ZERO),
                                   (100,200,ZERO),(10000,200,'0x'+'2'*40)):
            self.assertIsNone(self.exclusion('buyingBlocks',True,fee=fee,spacing=spacing,hooks=hooks))

    def test_legacy_preserved(self):
        self.assertIsNone(self.exclusion(None,False,token=BANNED,fee=10000,spacing=200))
        self.assertEqual(self.exclusion(None,True,fee=10000,spacing=200),
                         'BANNED_V4_RUG_TEMPLATE_1PCT_200_ZERO_HOOK')
        self.assertEqual(self.exclusion(None,True,fee=1,spacing=1,flex=True),
                         'TEMP_BANNED_V4_FEE1_SPACING1_ZERO_HOOK')

if __name__=='__main__':unittest.main()
