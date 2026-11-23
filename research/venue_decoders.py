"""Strict ABI decoding. V3 pool and V4 caller deltas deliberately differ."""

V3_SWAP = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
V3_BURN = '0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c'
V4_SWAP = '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'
V4_LIQUIDITY = '0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec'
V4_MANAGER = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
# Set from published event signatures, independently keccak-checked by executable probe.
PONS_BUY = '0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455'
PONS_SELL = '0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df'


def uint(word, bits=256):
    n = int(word, 16)
    if not 0 <= n < 2**bits:
        raise ValueError('Noncanonical unsigned ABI integer')
    return n


def sint(word, bits=256):
    n = int(word, 16)
    low = n & ((1 << bits) - 1)
    value = low - (1 << bits) if low & (1 << (bits - 1)) else low
    if n != value % (1 << 256):
        raise ValueError('Noncanonical signed ABI integer')
    return value


def words(raw, count):
    data = raw.get('data', '')
    if (
        not isinstance(data, str)
        or not data.startswith('0x')
        or len(data) != 2 + count * 64
    ):
        raise ValueError('Unexpected event ABI length')
    result = [data[2 + i * 64 : 2 + (i + 1) * 64] for i in range(count)]
    for w in result:
        uint(w)
    return result


def decodeLog(raw, venue):
    topics = raw.get('topics', [])
    if not topics or any(
        not isinstance(t, str) or len(t) != 66 or not t.startswith('0x') for t in topics
    ):
        raise ValueError('Malformed topics')
    for t in topics:
        uint(t[2:])
    source = venue['source']
    topic = topics[0].lower()
    emitter = V4_MANAGER if source == 'uniswapV4' else venue['id']
    if raw.get('address', '').lower() != emitter.lower():
        raise ValueError('Wrong emitter')
    if source == 'uniswapV4' and (
        len(topics) < 2 or topics[1].lower() != venue['id'].lower()
    ):
        raise ValueError('Wrong pool ID')
    for key in ('blockHash', 'transactionHash'):
        value = raw.get(key, '')
        if not isinstance(value, str) or len(value) != 66 or not value.startswith('0x'):
            raise ValueError('Missing chain identity')
        uint(value[2:])
    result = dict(
        transactionHash=raw['transactionHash'].lower(),
        blockHash=raw['blockHash'].lower(),
        blockNumber=int(raw['blockNumber'], 16),
        logIndex=int(raw['logIndex'], 16),
        coreOnly=source == 'uniswapV4',
    )
    if source == 'uniswapV3' and topic == V3_SWAP:
        if len(topics) != 3:
            raise ValueError('Unexpected v3 Swap topics')
        w = words(raw, 5)
        a0 = sint(w[0])
        a1 = sint(w[1])
        uint(w[2], 160)
        uint(w[3], 128)
        sint(w[4], 24)
        base = a1 if venue['baseAsset'] == venue['token1'] else a0
        direction = base
    elif source == 'uniswapV4' and topic == V4_SWAP:
        if len(topics) != 3:
            raise ValueError('Unexpected v4 Swap topics')
        w = words(raw, 6)
        a0 = sint(w[0], 128)
        a1 = sint(w[1], 128)
        uint(w[2], 160)
        uint(w[3], 128)
        sint(w[4], 24)
        result['coreFee'] = uint(w[5], 24)
        base = a1 if venue['baseAsset'] == venue['token1'] else a0
        direction = -base
    elif source == 'ponsV2' and topic in (PONS_BUY, PONS_SELL):
        if len(topics) != 3:
            raise ValueError('Unexpected Pons topics')
        w = words(raw, 4)
        amounts = list(map(uint, w))
        fee, tax = amounts[2:]
        if topic == PONS_BUY:
            direction = amounts[0] - fee - tax
            if direction < 0:
                raise ValueError('Pons deductions exceed quoteIn')
        else:
            direction = -(amounts[1] + fee + tax)
        result.update(
            protocolSemanticsUnverified=True,
            fee=str(fee),
            tax=str(tax),
            quoteRaw=str(amounts[0 if topic == PONS_BUY else 1]),
        )
    elif source == 'uniswapV3' and topic == V3_BURN:
        if len(topics) != 4:
            raise ValueError('Unexpected v3 Burn topics')
        sint(topics[2][2:], 24)
        sint(topics[3][2:], 24)
        w = words(raw, 3)
        result.update(
            kind='liquidity',
            liquidityRemoved=uint(w[0], 128) > 0,
            liquidityDelta=str(-uint(w[0], 128)),
        )
        return result
    elif source == 'uniswapV4' and topic == V4_LIQUIDITY:
        if len(topics) != 3:
            raise ValueError('Unexpected v4 liquidity topics')
        w = words(raw, 4)
        sint(w[0], 24)
        sint(w[1], 24)
        delta = sint(w[2])
        result.update(
            kind='liquidity', liquidityRemoved=delta < 0, liquidityDelta=str(delta)
        )
        return result
    else:
        raise ValueError('Unsupported venue/topic')
    result.update(
        kind='trade',
        buyBaseAmount=str(max(direction, 0)),
        sellBaseAmount=str(max(-direction, 0)),
    )
    return result


def activityFilter(venue):
    if venue['source'] == 'uniswapV4':
        return dict(address=V4_MANAGER, topics=[[V4_SWAP, V4_LIQUIDITY], venue['id']])
    topics = (
        [PONS_BUY, PONS_SELL] if venue['source'] == 'ponsV2' else [V3_SWAP, V3_BURN]
    )
    return dict(address=venue['id'], topics=[topics])