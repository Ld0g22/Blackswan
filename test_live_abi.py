"""Real eth-abi/eth-utils transaction encoding tests, no RPC, keys or trading."""
import unittest

from eth_abi import decode
from eth_utils import keccak
import peak_tracker as peak
from swap_adapters import (buildV3,buildV4,buildPons,NATIVE,WETH,USDG,
                           V3_ROUTER,V4_ROUTER)

TOKEN='0x'+'a'*40
WALLET='0x'+'b'*40
HOOK='0x'+'c'*40


def decodeCall(sig,types,calldata):
    expected=keccak(text=sig)[:4]
    assert bytes.fromhex(calldata[2:10])==expected, (sig,calldata[:10])
    return decode(types,bytes.fromhex(calldata[10:]))


class TestRealAbi(unittest.TestCase):
    def test_v3_router02_exact_input(self):
        venue=dict(source='uniswapV3',token0=min(TOKEN,WETH),token1=max(TOKEN,WETH),fee=3000)
        tx=buildV3(venue,NATIVE,10000,800,WALLET)
        self.assertEqual(tx['to'],V3_ROUTER)
        self.assertEqual(tx['value'],10000)
        params=decodeCall('exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))',
             ['(address,address,uint24,address,uint256,uint256,uint160)'],tx['data'])[0]
        self.assertEqual(tuple(a.lower() if isinstance(a,str) else a for a in params),
                         (WETH,TOKEN,3000,WALLET,10000,800,0))
        sell=buildV3(venue,TOKEN,500,50,WALLET)
        self.assertEqual(sell['value'],0)
        self.assertEqual(decodeCall('exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))',
            ['(address,address,uint24,address,uint256,uint256,uint160)'],sell['data'])[0][1].lower(),WETH)

    def test_pons_native_curve(self):
        curve='0x'+'d'*40
        venue=dict(source='ponsV2',id=curve,emitter=curve,token0=TOKEN,token1=NATIVE)
        buy=buildPons(venue,NATIVE,12345,1000,WALLET)
        self.assertEqual(buy['to'],curve)
        self.assertEqual(buy['value'],12345)
        self.assertEqual(decodeCall('buy(uint256,uint256,address)',
            ['uint256','uint256','address'],buy['data']), (12345,1000,WALLET))
        sell=buildPons(venue,TOKEN,80,90,WALLET)
        self.assertEqual(sell['value'],0)
        self.assertEqual(decodeCall('sell(uint256,uint256,address)',
            ['uint256','uint256','address'],sell['data']), (80,90,WALLET))

    def test_v4_native_single_payload(self):
        venue=dict(source='uniswapV4',token0=NATIVE,token1=TOKEN,fee=500,spacing=10,
                   hooks=HOOK)
        tx=buildV4(venue,NATIVE,1000,75,WALLET)
        self.assertEqual(tx['to'],V4_ROUTER)
        self.assertEqual(tx['value'],1000)
        commands,inputs,deadline=decodeCall('execute(bytes,bytes[],uint256)',
                ['bytes','bytes[]','uint256'],tx['data'])
        self.assertEqual(commands,b'\x10')
        actions,params=decode(['bytes','bytes[]'],inputs[0])
        self.assertEqual(actions,b'\x06\x0c\x0f')
        swap=decode(['((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)'],
                    params[0])[0]
        self.assertEqual(swap[0][0].lower(),NATIVE)
        self.assertEqual(swap[0][1].lower(),TOKEN)
        self.assertEqual(swap[1],True)
        self.assertEqual(swap[2:5],(1000,75,0))
        self.assertEqual(swap[5],b'')
        self.assertEqual(decode(['address','uint256'],params[1]),(NATIVE,1000))
        self.assertEqual(decode(['address','uint256'],params[2]),(TOKEN,75))

    def test_v4_sell_slippage_allows_drop_from_3x_to_2x(self):
        from swap_adapters import SwapAdapters
        venue=dict(source='uniswapV4',token0=NATIVE,token1=TOKEN,
                   fee=500,spacing=10,hooks=NATIVE)
        quote=dict(venue=venue,tokenIn=TOKEN,output=300)
        tx=SwapAdapters.build(object.__new__(SwapAdapters),quote,500,WALLET)
        commands,inputs,_=decodeCall('execute(bytes,bytes[],uint256)',
            ['bytes','bytes[]','uint256'],tx['data'])
        self.assertEqual(commands,b'\x10')
        actions,params=decode(['bytes','bytes[]'],inputs[0])
        self.assertEqual(actions,b'\x06\x0c\x0f')
        swap=decode(['((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)'],
                    params[0])[0]
        self.assertEqual(swap[1],False)
        self.assertEqual(swap[3],198) # 66% of quote: permits 3x -> 2x
        self.assertEqual(decode(['address','uint256'],params[2]),(NATIVE,198))
        self.assertEqual(tx['value'],0)

    def test_v4_eth_to_usdg_to_token(self):
        venue=dict(source='uniswapV4',token0=USDG,token1=TOKEN,fee=0,
                   spacing=60,hooks=HOOK)
        path=[(USDG,500,10,NATIVE,b''),(TOKEN,0,60,HOOK,b'')]
        tx=buildV4(venue,NATIVE,1000,50,WALLET,path)
        commands,inputs,_=decodeCall('execute(bytes,bytes[],uint256)',
                ['bytes','bytes[]','uint256'],tx['data'])
        actions,params=decode(['bytes','bytes[]'],inputs[0])
        self.assertEqual(actions,b'\x07\x0c\x0f')
        struct=decode(['(address,(address,uint24,int24,address,bytes)[],uint256[],uint128,uint128)'],
                      params[0])[0]
        self.assertEqual(struct[0].lower(),NATIVE)
        self.assertEqual([p[0].lower() for p in struct[1]],[USDG,TOKEN])
        self.assertEqual(struct[2],())
        self.assertEqual(struct[3:],(1000,50))

    def test_v4_token_to_usdg_to_native_sell_payload(self):
        from swap_adapters import SwapAdapters, NATIVE
        venue=dict(source='uniswapV4',token0=USDG,token1=TOKEN,fee=0,
                   spacing=60,hooks=HOOK)
        path=[(USDG,0,60,HOOK,b''),(NATIVE,500,10,NATIVE,b'')]
        quote=dict(venue=venue,tokenIn=TOKEN,output=300,path=path)
        tx=SwapAdapters.build(object.__new__(SwapAdapters),quote,500,WALLET)
        commands,inputs,_=decodeCall('execute(bytes,bytes[],uint256)',
              ['bytes','bytes[]','uint256'],tx['data'])
        self.assertEqual(commands,b'\x10')
        actions,params=decode(['bytes','bytes[]'],inputs[0])
        self.assertEqual(actions,b'\x07\x0c\x0f')
        struct=decode(['(address,(address,uint24,int24,address,bytes)[],uint256[],uint128,uint128)'],
                      params[0])[0]
        self.assertEqual(struct[0].lower(),TOKEN)
        self.assertEqual([p[0].lower() for p in struct[1]],[USDG,NATIVE])
        self.assertEqual(struct[3],500)
        self.assertEqual(struct[4],198)
        self.assertEqual(decode(['address','uint256'],params[1]),(TOKEN,500))
        self.assertEqual(decode(['address','uint256'],params[2]),(NATIVE,198))
        self.assertEqual(tx['value'],0)

    def test_v4_weth_input_wraps_native(self):
        venue=dict(source='uniswapV4',token0=WETH,token1=TOKEN,fee=500,spacing=10,
                   hooks=NATIVE)
        tx=buildV4(venue,WETH,300,40,WALLET)
        commands,inputs,_=decodeCall('execute(bytes,bytes[],uint256)',
              ['bytes','bytes[]','uint256'],tx['data'])
        self.assertEqual(commands,b'\x0b\x10')
        self.assertEqual(tx['value'],300)
        actions,params=decode(['bytes','bytes[]'],inputs[1])
        self.assertEqual(actions,b'\x06\x0b\x0f')
        self.assertEqual(decode(['address','uint256','bool'],params[1]),(WETH,300,False))


if __name__=='__main__':unittest.main()