"""
Pool objects - one class per Uniswap version, one shared interface.

The point of this module is that "what is the price?" and "what do I get for X?"
are the same questions on V2, V3 and V4, even though the on-chain answer comes from
three completely different places (reserves / slot0 / a shared PoolManager). Code
using this library asks the question once and does not branch on the version.

Metadata (which tokens, their decimals, a V4 pool's key) is read once when the pool
is loaded. Only the price itself is re-read on every call, which keeps polling cheap.
"""
from web3 import Web3

from . import abis, pricing
from .chains import NATIVE_ADDRESS


def fetch_token_info(w3, address, chain=None):
    """
    Read a token's symbol and decimals from the chain.

    The zero address means the chain's native coin, which has no ERC20 contract to
    call - so it is answered from config instead of on-chain.
    """
    if address.lower() == NATIVE_ADDRESS:
        return (chain.native_symbol if chain else "NATIVE"), 18
    token = w3.eth.contract(address=Web3.to_checksum_address(address), abi=abis.ERC20_ABI)
    return token.functions.symbol().call().upper(), token.functions.decimals().call()


def detect_pool_type(w3, pool_address):
    """
    Work out whether an address is a V2 pair or a V3 pool.

    Both expose token0()/token1(), so those cannot tell them apart. fee() exists
    only on V3 and getReserves() only on V2, so probing those does distinguish them.
    Returns "v2" or "v3".
    """
    address = Web3.to_checksum_address(pool_address)

    v3 = w3.eth.contract(address=address, abi=abis.V3_POOL_ABI)
    try:
        v3.functions.fee().call()
        return "v3"
    except Exception:
        pass

    v2 = w3.eth.contract(address=address, abi=abis.V2_PAIR_ABI)
    try:
        v2.functions.getReserves().call()
        return "v2"
    except Exception:
        pass

    raise ValueError(
        f"{pool_address} icin pool tipi tespit edilemedi "
        "(ne fee() ne getReserves() calisti - bu adres bir pool olmayabilir)"
    )


def get_pool_manager(w3, state_view_address):
    """Ask StateView which PoolManager it reads from, instead of hardcoding it per chain."""
    state_view = w3.eth.contract(
        address=Web3.to_checksum_address(state_view_address), abi=abis.STATE_VIEW_ABI
    )
    return state_view.functions.poolManager().call()


def pool_key_from_position_manager(w3, position_manager, pool_id):
    """
    Ask PositionManager for the PoolKey behind a pool id.

    The id is keccak of the key and cannot be reversed, but the contract kept the
    key when the pool was created: poolKeys() is a mapping from the first 25 bytes
    of the id to the whole struct. One eth_call, and it works on any endpoint -
    unlike the Initialize event, which needs a log query most RPCs will not serve.

    Returns (currency0, currency1, fee, tick_spacing, hooks), or None when the
    contract has never seen this pool.
    """
    selector = Web3.keccak(text="poolKeys(bytes25)")[:4]
    # bytes25 is left-aligned in its word, so the tail is padding rather than data.
    key = Web3.to_bytes(hexstr=pool_id)[:25].ljust(32, b"\x00")
    raw = w3.eth.call({"to": Web3.to_checksum_address(position_manager),
                       "data": selector + key})
    currency0, currency1, fee, tick_spacing, hooks = w3.codec.decode(
        ["address", "address", "uint24", "int24", "address"], raw)
    # An unknown id decodes to zeroes rather than reverting; both currencies being
    # zero is impossible for a real pool, since currency0 < currency1.
    if int(currency0, 16) == 0 and int(currency1, 16) == 0:
        return None
    return currency0, currency1, fee, tick_spacing, hooks


def pool_key_from_logs(w3, state_view_address, pool_id, from_block=0):
    """
    Recover a PoolKey from the Initialize event PoolManager emits at creation.

    The fallback for chains with no PositionManager configured. It needs a log query
    over the whole chain, which many endpoints refuse outright and none serve
    quickly - hence the fallback rather than the default.
    """
    pool_manager = get_pool_manager(w3, state_view_address)
    topic0 = Web3.keccak(text=abis.V4_INITIALIZE_EVENT)

    logs = w3.eth.get_logs({
        "address": pool_manager,
        "fromBlock": from_block,
        "toBlock": "latest",
        "topics": [topic0, Web3.to_bytes(hexstr=pool_id)],
    })
    if not logs:
        raise ValueError(
            f"{pool_id} icin Initialize event'i bulunamadi "
            "(yanlis pool_id, yanlis chain, ya da from_block cok ileride olabilir)"
        )

    log = logs[0]
    currency0 = w3.codec.decode(["address"], log["topics"][2])[0]
    currency1 = w3.codec.decode(["address"], log["topics"][3])[0]
    fee, tick_spacing, hooks, _sqrt_price, _tick = w3.codec.decode(
        ["uint24", "int24", "address", "uint160", "int24"], log["data"]
    )
    return currency0, currency1, fee, tick_spacing, hooks


# Uniswap's own tiers plus the ones launchpads use here. Only ever tried against a
# hash that has to match exactly, so a wrong pair is rejected rather than accepted.
_FEE_CANDIDATES = (0, 100, 200, 400, 500, 800, 1000, 2500, 3000, 4000, 5000,
                   10000, 15000, 20000, 25000, 30000, 8388608)
# Walked rather than listed. Tick spacing is a free integer and pools do use values
# nobody would think to enumerate - one here is 25 - while a keccak costs nothing, so
# the range is swept instead of guessed at.
_TICK_CANDIDATES = tuple(range(1, 1001)) + (60000,)
_V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


_INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"


def pool_key_by_bisect(w3, chain, pool_id, window=4):
    """
    Find the block a pool was created in, then read its key out of that block.

    The Initialize event carries the whole PoolKey and is the only thing that ever
    did - nothing on-chain stores it afterwards. Reading it means knowing where to
    look, and scanning for it is what every endpoint refuses: a free tier caps log
    queries at ten or ten thousand blocks, against a chain millions deep.

    Nothing has to be scanned. getSlot0 answers zero for a pool that does not exist
    yet and something for one that does, so the creation block is a boundary and a
    bisection walks to it in about as many calls as the chain has bits - two dozen
    here. Then a single log query over a handful of blocks reads the event that any
    endpoint will serve.

    Needs an archive node, since the calls are historical. Where the node has pruned
    them this raises and the caller falls through to guessing instead - but where it
    has not, this is exact: the key comes from the event rather than from a search,
    and is checked against the pool id before it is returned.
    """
    state_view = w3.eth.contract(
        address=Web3.to_checksum_address(chain.state_view), abi=abis.STATE_VIEW_ABI)
    manager = state_view.functions.poolManager().call()
    raw_id = bytes.fromhex(pool_id.lower().removeprefix("0x"))

    def state_at(block):
        return state_view.functions.getSlot0(raw_id).call(block_identifier=block)[0] != 0

    def initialised(block):
        # A block before the contract existed answers with a revert rather than a
        # zero, and that is a "not yet" like any other as far as the search cares.
        try:
            return state_at(block)
        except Exception:
            return False

    latest = w3.eth.block_number
    if not initialised(latest):
        raise ValueError(f"{pool_id} bu zincirde kurulu degil")

    # A pruning node refuses a call this far back, which the search would read as
    # "not yet" all the way to the tip and then find no event there. Saying so is
    # better than walking two dozen calls into a wrong answer.
    try:
        state_at(max(1, latest // 2))
    except Exception as error:
        raise ValueError("node gecmisi budamis - bisect kullanilamaz") from error

    low, high = 0, latest
    while low < high:
        middle = (low + high) // 2
        if initialised(middle):
            high = middle
        else:
            low = middle + 1

    # The bisection lands on the first block where the pool exists, which is the
    # block it was created in - so one block is the whole query. Widening is a
    # fallback for a chain that reorganised around the boundary, and stays small:
    # an endpoint that caps log queries at ten blocks is exactly the kind this
    # function exists for.
    logs = []
    for span in (0, window):
        try:
            logs = w3.eth.get_logs({
                "address": Web3.to_checksum_address(manager),
                "topics": [_INITIALIZE_TOPIC, pool_id],
                "fromBlock": max(0, low - span),
                "toBlock": low + span,
            })
        except Exception:
            continue
        if logs:
            break
    if not logs:
        raise ValueError(f"{pool_id} icin Initialize event blok {low} civarinda bulunamadi")

    entry = logs[0]
    topics = [t.hex() if hasattr(t, "hex") else str(t) for t in entry["topics"]]
    currency0 = Web3.to_checksum_address("0x" + topics[2].removeprefix("0x")[-40:])
    currency1 = Web3.to_checksum_address("0x" + topics[3].removeprefix("0x")[-40:])
    data = bytes(entry["data"])
    fee = int.from_bytes(data[0:32], "big")
    tick_spacing = int.from_bytes(data[32:64], "big", signed=True)
    hooks = Web3.to_checksum_address("0x" + data[64:96].hex()[-40:])

    # The event is trusted no further than the id it has to reproduce.
    encoded = w3.codec.encode(
        ["address", "address", "uint24", "int24", "address"],
        [currency0, currency1, fee, tick_spacing, hooks])
    if Web3.keccak(encoded).hex().lower().removeprefix("0x") != pool_id.lower().removeprefix("0x"):
        raise ValueError(f"{pool_id} icin okunan PoolKey id ile tutmadi")
    return currency0, currency1, fee, tick_spacing, hooks


def _addresses_from_recent_swaps(w3, chain, pool_id, windows=(2000, 10000, 50000)):
    """
    Currencies and hook candidates from whatever swaps the node still remembers.

    Returns (currencies, others). Both can come back short: V4 settles inside the
    PoolManager, so a currency that never leaves it emits no Transfer, and a pool
    whose trades are routed through several hops shows only the ends of the route.
    """
    state_view = w3.eth.contract(
        address=Web3.to_checksum_address(chain.state_view), abi=abis.STATE_VIEW_ABI)
    manager = state_view.functions.poolManager().call()

    logs = []
    latest = w3.eth.block_number
    for window in windows:
        try:
            logs = w3.eth.get_logs({
                "address": Web3.to_checksum_address(manager),
                "topics": [_V4_SWAP_TOPIC, pool_id],
                "fromBlock": max(0, latest - window),
                "toBlock": "latest",
            })
        except Exception:
            continue
        if logs:
            break
    if not logs:
        return [], []

    # Sampled across the window rather than taken from the end, and a transaction
    # that swapped this pool ALONE is worth far more than one that routed through
    # it: only there do the Transfer logs belong to this pool rather than to the
    # ends of somebody's route. So single-swap transactions are read first and the
    # search stops at the first one found.
    step = max(1, len(logs) // 25)
    sample = logs[::step][:25]
    currencies, others, seen = [], [], set()
    for log in sample:
        tx_hash = log["transactionHash"]
        if tx_hash in seen:
            continue
        seen.add(tx_hash)
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        entries = receipt["logs"]
        swaps = sum(1 for e in entries
                    if e["topics"] and _topic0(e) == _V4_SWAP_TOPIC)
        alone = swaps == 1
        for entry in entries:
            address = entry["address"].lower()
            if _topic0(entry) == _TRANSFER_TOPIC:
                if address not in currencies:
                    currencies.append(address)
            elif address != manager.lower() and address not in others:
                others.append(address)
        if alone:
            break
    return currencies, others


def _topic0(entry):
    if not entry["topics"]:
        return ""
    topic = entry["topics"][0]
    topic = topic.hex() if hasattr(topic, "hex") else str(topic)
    topic = topic.lower()
    return topic if topic.startswith("0x") else "0x" + topic


def find_pool_key(w3, chain, pool_id, currencies, hooks=()):
    """
    Search for a PoolKey with the currencies already known.

    The automatic route fails on pools whose trades are routed: there is no Transfer
    to read a currency off when V4 never moves it out of the manager. Told which two
    they are - from a pool page, say - what is left is the fee, the tick spacing and
    the hook, and that is small enough to walk through.

    The pool id still decides. It is keccak of the encoded key, so a candidate that
    hashes to it IS the key; a wrong one never matches and this returns None rather
    than something plausible.

    Hook candidates are whatever the caller passes plus the addresses seen alongside
    recent swaps, and the zero address for a pool without one.
    """
    candidates = [NATIVE_ADDRESS] + [h.lower() for h in hooks if h]
    try:
        _, others = _addresses_from_recent_swaps(w3, chain, pool_id)
    except Exception:
        others = []
    candidates += [a for a in others if a not in candidates]

    currencies = [c.lower() for c in currencies]
    pairs = [(a, b) for i, a in enumerate(currencies) for b in currencies[i + 1:]]
    target = pool_id.lower().removeprefix("0x")
    for hook in candidates:
        for first, second in pairs:
            c0, c1 = sorted([first, second])
            for fee in _FEE_CANDIDATES:
                for spacing in _TICK_CANDIDATES:
                    encoded = w3.codec.encode(
                        ["address", "address", "uint24", "int24", "address"],
                        [Web3.to_checksum_address(c0), Web3.to_checksum_address(c1),
                         fee, spacing, Web3.to_checksum_address(hook)])
                    if Web3.keccak(encoded).hex().lower().removeprefix("0x") == target:
                        return (Web3.to_checksum_address(c0),
                                Web3.to_checksum_address(c1), fee, spacing,
                                Web3.to_checksum_address(hook))
    return None


def pool_key_from_swaps(w3, chain, pool_id, windows=(2000, 10000, 50000)):
    """
    Rebuild a PoolKey from a recent swap, on a chain whose history cannot be read.

    The usual routes both need something this kind of endpoint will not give: the
    PositionManager knows only pools liquidity has passed through, and the Initialize
    event sits far enough back that a pruning node answers "pruned history
    unavailable". What such a node will still serve is the recent past.

    So the key is reconstructed rather than looked up. One recent swap on this pool
    names the transaction; the transaction's Transfer logs name the currencies, and
    its remaining log addresses are the only plausible hooks. Fee and tick spacing
    come from a short list of the values anyone actually deploys.

    The reconstruction proves itself. A pool id is keccak of the encoded PoolKey, so
    a candidate that hashes to the id IS the key - there is no near-miss and nothing
    to trust. That is the whole reason this is allowed to guess at all: every guess
    is checked against the hash, and a wrong one simply never matches.

    Returns (currency0, currency1, fee, tick_spacing, hooks), or raises.
    """
    currencies, others = _addresses_from_recent_swaps(w3, chain, pool_id, windows)
    if not currencies:
        raise ValueError(
            f"{pool_id} icin yakin gecmiste swap yok - PoolKey kurulamiyor. "
            "Havuz islem gormeye baslayinca tekrar deneyin")
    # The native coin never shows as a Transfer - it is not a token - so it has to be
    # offered as a candidate rather than discovered.
    currencies = currencies + [NATIVE_ADDRESS]
    hooks = [NATIVE_ADDRESS] + others

    pairs = [(a, b) for i, a in enumerate(currencies) for b in currencies[i + 1:]]
    target = pool_id.lower()
    for hook in hooks:
        for first, second in pairs:
            c0, c1 = sorted([first.lower(), second.lower()])
            for fee in _FEE_CANDIDATES:
                for spacing in _TICK_CANDIDATES:
                    encoded = w3.codec.encode(
                        ["address", "address", "uint24", "int24", "address"],
                        [Web3.to_checksum_address(c0), Web3.to_checksum_address(c1),
                         fee, spacing, Web3.to_checksum_address(hook)])
                    digest = Web3.keccak(encoded).hex().lower()
                    if digest.removeprefix("0x") == target.removeprefix("0x"):
                        return (Web3.to_checksum_address(c0),
                                Web3.to_checksum_address(c1), fee, spacing,
                                Web3.to_checksum_address(hook))
    raise ValueError(
        f"{pool_id} icin PoolKey kurulamadi - fee/tickSpacing/hook bu havuzda "
        "alisildik degerlerin disinda olabilir")


def fetch_v4_pool_key(w3, chain, pool_id, from_block=0):
    """
    The PoolKey behind a pool id, by whichever route this chain allows.

    PositionManager first: one call, no history, no endpoint requirements. Falling
    back to the Initialize event only when the chain has no PositionManager set, or
    when it has not heard of this pool.

    Returns (currency0, currency1, fee, tick_spacing, hooks).
    """
    if chain.position_manager:
        try:
            key = pool_key_from_position_manager(w3, chain.position_manager, pool_id)
            if key:
                return key
        except Exception:
            pass                       # fall through to the slower way

    try:
        return pool_key_from_logs(w3, chain.state_view, pool_id, from_block)
    except Exception:
        pass

    # Scanning refused. On an archive node the event can still be reached without
    # scanning at all, by bisecting to the block the pool was created in.
    try:
        return pool_key_by_bisect(w3, chain, pool_id)
    except Exception:
        pass

    # No history to read. What is left is rebuilding the key from a recent swap and
    # checking it against the id - a search, but one the id itself adjudicates.
    return pool_key_from_swaps(w3, chain, pool_id)


class Pool:
    """
    Shared interface for every pool version.

    A pool has two sides. In practice one of them is almost always a base asset
    (native coin, wrapped native, a stablecoin) and the other is the token being
    tracked - so this class resolves that once and then talks in terms of "token"
    and "base" rather than the raw token0/token1 ordering, which is just an
    address-sorting artifact and flips unpredictably between pools.
    """

    pool_type = None
    exact_quotes = False  # True only where a closed-form formula exists (V2)

    def __init__(self, w3, chain, token0, token1, decimals0, decimals1, symbol0, symbol1):
        self.w3 = w3
        self.chain = chain
        self.token0 = token0
        self.token1 = token1
        self.decimals0 = decimals0
        self.decimals1 = decimals1
        self.symbol0 = symbol0
        self.symbol1 = symbol1

        base0 = chain.is_base_token(token0)
        base1 = chain.is_base_token(token1)
        if base0 and not base1:
            self.token_is_0 = False
        elif base1 and not base0:
            self.token_is_0 = True
        else:
            # Both sides base (e.g. ETH/USDC) or neither - there is no single
            # obvious "tracked token", so refuse to guess. quote() still works.
            self.token_is_0 = None

    # -- identity -----------------------------------------------------------

    @property
    def token(self):
        """Address of the tracked (non-base) token."""
        self._require_resolved()
        return self.token0 if self.token_is_0 else self.token1

    @property
    def base(self):
        """Address of the base/quote asset."""
        self._require_resolved()
        return self.token1 if self.token_is_0 else self.token0

    @property
    def symbol(self):
        self._require_resolved()
        return self.symbol0 if self.token_is_0 else self.symbol1

    @property
    def base_symbol(self):
        self._require_resolved()
        return self.symbol1 if self.token_is_0 else self.symbol0

    @property
    def decimals(self):
        self._require_resolved()
        return self.decimals0 if self.token_is_0 else self.decimals1

    @property
    def base_decimals(self):
        self._require_resolved()
        return self.decimals1 if self.token_is_0 else self.decimals0

    def _require_resolved(self):
        if self.token_is_0 is None:
            raise ValueError(
                f"Bu pool'da hangi tarafin takip edilen token oldugu belirsiz "
                f"({self.symbol0}/{self.symbol1}). Acik olan quote() metodunu kullan."
            )

    # -- prices -------------------------------------------------------------

    def price_call(self):
        """
        (target, calldata) for the one call that carries this pool's price.

        Split out from _prices so the same encoding serves both ways of asking. Read
        one pool and it goes out on its own; read forty and they go into a single
        Multicall3 request. If the two described the call separately they would drift,
        and the batched path would quietly answer about something else.
        """
        raise NotImplementedError

    def decode_prices(self, data):
        """(price_0, price_1) from whatever price_call() returns."""
        raise NotImplementedError

    def _prices(self, block=None):
        """
        (price_0, price_1) for this pool.

        `block` pins the read. Comparing two figures that came from different blocks
        is how a cost ends up negative: the pool moved between the calls and the
        difference stopped being about cost at all.
        """
        target, data = self.price_call()
        raw = self.w3.eth.call({"to": target, "data": data},
                               block_identifier=block or "latest")
        return self.decode_prices(raw)

    def price(self):
        """Current price of the tracked token, expressed in the base asset."""
        price_0, price_1 = self._prices()
        return price_0 if self.token_is_0 else price_1

    def base_price(self):
        """Current price of the base asset, expressed in the tracked token."""
        price_0, price_1 = self._prices()
        return price_1 if self.token_is_0 else price_0

    # -- quotes -------------------------------------------------------------

    def quote_sell(self, amount_in):
        """How much of the base asset selling `amount_in` tracked tokens yields."""
        self._require_resolved()
        return self.quote(self.token, amount_in)

    def quote_buy(self, amount_in):
        """How many tracked tokens spending `amount_in` of the base asset yields."""
        self._require_resolved()
        return self.quote(self.base, amount_in)

    def quote(self, token_in, amount_in, block=None):
        """
        Output for swapping `amount_in` of `token_in` into the other side.

        Unambiguous by construction - use this when a pool has no obvious base side.
        Unless exact_quotes is True this is a spot-price estimate: it ignores price
        impact and fees, so it reads high on anything but small trades.
        """
        price_0, price_1 = self._prices(block)
        if token_in.lower() == self.token0.lower():
            return amount_in * price_0
        elif token_in.lower() == self.token1.lower():
            return amount_in * price_1
        raise ValueError(f"{token_in} bu pool'un tokenlarindan biri degil")

    def metadata(self):
        """
        Everything about this pool that does not change, as a plain dict.

        Pairs with restore_pool(): save this once when a pool is first added, and
        later rebuilds cost no chain reads at all. Discovering a pool takes several
        sequential RPC calls - probing the version, reading token0/token1, then symbol
        and decimals for each side - and none of those answers ever change.

        Only the price is worth re-reading, and that is one call.
        """
        return {
            "pool_type": self.pool_type,
            "identifier": self.identifier,
            "token0": self.token0,
            "token1": self.token1,
            "decimals0": self.decimals0,
            "decimals1": self.decimals1,
            "symbol0": self.symbol0,
            "symbol1": self.symbol1,
        }

    def __repr__(self):
        pair = f"{self.symbol0}/{self.symbol1}"
        return f"<{type(self).__name__} {pair}>"


class V2Pool(Pool):
    """
    Constant-product pair. Each pool is its own contract holding two reserves.

    The only version where an exact quote is possible off-chain: the output of a
    trade has a closed-form formula, so fees and price impact are both accounted for
    without simulating anything.
    """

    pool_type = "v2"
    exact_quotes = True

    @property
    def identifier(self):
        return self.address

    def __init__(self, w3, chain, address, **kwargs):
        self.address = Web3.to_checksum_address(address)
        self.contract = w3.eth.contract(address=self.address, abi=abis.V2_PAIR_ABI)
        super().__init__(w3, chain, **kwargs)

    def reserves(self, block=None):
        reserve0, reserve1, _ = self.contract.functions.getReserves().call(
            block_identifier=block or "latest")
        return reserve0, reserve1

    def price_call(self):
        return self.address, Web3.keccak(text="getReserves()")[:4].hex()

    def decode_prices(self, data):
        reserve0, reserve1, _ = self.w3.codec.decode(
            ["uint112", "uint112", "uint32"], data)
        amount0 = reserve0 / 10**self.decimals0
        amount1 = reserve1 / 10**self.decimals1
        price_0 = amount1 / amount0
        return price_0, 1 / price_0

    def quote(self, token_in, amount_in, block=None):
        """Exact output, including the pool's fee and the price impact of this trade."""
        reserve0, reserve1 = self.reserves(block)

        if token_in.lower() == self.token0.lower():
            reserve_in, reserve_out = reserve0, reserve1
            decimals_in, decimals_out = self.decimals0, self.decimals1
        elif token_in.lower() == self.token1.lower():
            reserve_in, reserve_out = reserve1, reserve0
            decimals_in, decimals_out = self.decimals1, self.decimals0
        else:
            raise ValueError(f"{token_in} bu pool'un tokenlarindan biri degil")

        amount_out_wei = pricing.v2_amount_out(
            int(amount_in * 10**decimals_in),
            reserve_in,
            reserve_out,
            self.chain.v2_fee_numerator,
            self.chain.v2_fee_denominator,
        )
        return amount_out_wei / 10**decimals_out


class V3Pool(Pool):
    """
    Concentrated liquidity. Still one contract per pool, price held in slot0.

    No closed-form quote exists here: liquidity can change at every tick boundary,
    so the true output of a trade is only knowable by walking those ticks. Quotes
    from this class are spot-price estimates.
    """

    pool_type = "v3"

    @property
    def identifier(self):
        return self.address

    def metadata(self):
        return {**super().metadata(), "fee": self.fee}

    def __init__(self, w3, chain, address, fee=None, **kwargs):
        self.address = Web3.to_checksum_address(address)
        self.contract = w3.eth.contract(address=self.address, abi=abis.V3_POOL_ABI)
        self.fee = fee if fee is not None else self.contract.functions.fee().call()
        super().__init__(w3, chain, **kwargs)

    def price_call(self):
        return self.address, Web3.keccak(text="slot0()")[:4].hex()

    def decode_prices(self, data):
        """
        Only the first two fields are read, on purpose.

        Forks extend slot0. One pool on this chain returns eight values where
        Uniswap's returns seven, and decoding against the full Uniswap shape fails on
        the trailing bytes - turning a perfectly readable price into a decode error.
        sqrtPriceX96 and tick lead the struct in every variant seen, and nothing here
        reads past them, so whatever a fork appends is ignored rather than fatal.
        """
        sqrt_price_x96, _tick = self.w3.codec.decode(["uint160", "int24"], data[:64])
        return pricing.sqrt_price_x96_to_prices(sqrt_price_x96, self.decimals0, self.decimals1)

    def slot0(self, block=None):
        target, data = self.price_call()
        raw = self.w3.eth.call({"to": target, "data": data},
                               block_identifier=block or "latest")
        return self.w3.codec.decode(["uint160", "int24"], raw[:64])


class V4Pool(Pool):
    """
    Singleton architecture: pools have no address, they live inside one PoolManager
    and are identified by a bytes32 pool id. State is read through StateView.

    `hooks` being non-zero means a custom contract runs around swaps and may change
    the economics - a pool can report fee=0 while the hook charges its own cut. Treat
    quotes on hooked pools with extra suspicion.
    """

    pool_type = "v4"

    @property
    def identifier(self):
        return self.pool_id

    def metadata(self):
        return {**super().metadata(), "fee": self.fee,
                "tick_spacing": self.tick_spacing, "hooks": self.hooks}

    def __init__(self, w3, chain, pool_id, fee, tick_spacing, hooks, **kwargs):
        if not chain.state_view:
            raise ValueError(f"{chain.name} icin StateView adresi tanimli degil (V4 yok?)")
        self.pool_id = pool_id
        self.fee = fee
        self.tick_spacing = tick_spacing
        self.hooks = hooks
        self.state_view = w3.eth.contract(
            address=Web3.to_checksum_address(chain.state_view), abi=abis.STATE_VIEW_ABI
        )
        super().__init__(w3, chain, **kwargs)

    @property
    def has_hooks(self):
        return self.hooks.lower() != NATIVE_ADDRESS

    @property
    def pool_key(self):
        """The PoolKey tuple, in the order the contracts expect it."""
        return (
            Web3.to_checksum_address(self.token0),
            Web3.to_checksum_address(self.token1),
            self.fee,
            self.tick_spacing,
            Web3.to_checksum_address(self.hooks),
        )

    def price_call(self):
        # V4 pools have no address of their own; state comes from StateView, keyed by
        # the pool id - so the id travels in the calldata rather than the target.
        # pool_id arrives as the hex string the user pasted, so it is normalised to
        # bytes here rather than assumed to be either.
        return (Web3.to_checksum_address(self.chain.state_view),
                Web3.keccak(text="getSlot0(bytes32)")[:4]
                + Web3.to_bytes(hexstr=self.pool_id))

    def decode_prices(self, data):
        sqrt_price_x96, _tick = self.w3.codec.decode(["uint160", "int24"], data[:64])
        return pricing.sqrt_price_x96_to_prices(sqrt_price_x96, self.decimals0, self.decimals1)

    def slot0(self, block=None):
        return self.state_view.functions.getSlot0(self.pool_id).call(
            block_identifier=block or "latest")

    def quote_exact(self, token_in, amount_in, hook_data=b"", block=None):
        """
        Ask the V4 quoter what this swap really returns.

        Worth preferring over quote() on this version especially: a hooked pool can
        charge through the hook while reporting fee=0, so the spot price understates
        the cost. The quoter runs the actual swap logic, hook included.

        Needs chain.v4_quoter to be set. Returns None if the quoter is unavailable or
        the call reverts, so callers can fall back to the spot estimate.
        """
        if not self.chain.v4_quoter:
            return None

        # Checked rather than assumed: treating "not token0" as token1 turns a wrong
        # address into a confident answer for the opposite direction. On V4 the base
        # is often the native coin, so passing the wrapped address lands here.
        if token_in.lower() not in (self.token0.lower(), self.token1.lower()):
            raise ValueError(
                f"{token_in} is not in this pool ({self.symbol0}/{self.symbol1}). "
                "On V4 the base side may be the native coin - pass pool.base rather "
                "than the wrapped address."
            )

        zero_for_one = token_in.lower() == self.token0.lower()
        decimals_in = self.decimals0 if zero_for_one else self.decimals1
        decimals_out = self.decimals1 if zero_for_one else self.decimals0

        quoter = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.chain.v4_quoter), abi=abis.V4_QUOTER_ABI
        )
        params = (self.pool_key, zero_for_one, int(amount_in * 10**decimals_in), hook_data)
        try:
            amount_out, _gas = quoter.functions.quoteExactInputSingle(params).call(
                block_identifier=block or "latest")
            return amount_out / 10**decimals_out
        except Exception:
            return None


def load_pool(chain, identifier, w3=None, from_block=0, pool_key=None):
    """
    Load a pool from a V2/V3 address or a V4 pool id, working out everything else.

    The identifier's length says which it is: an address is 20 bytes (40 hex chars),
    a V4 pool id is 32 bytes (64). For addresses the version is probed on-chain; for
    pool ids the PoolKey is recovered from the Initialize event.

    Pass an existing `w3` when polling repeatedly - otherwise a new connection is
    opened (and the chain id verified) on every call.
    """
    w3 = w3 or chain.connect()
    hex_part = identifier[2:] if identifier.lower().startswith("0x") else identifier

    if len(hex_part) == 40:
        pool_type = detect_pool_type(w3, identifier)
        cls = V2Pool if pool_type == "v2" else V3Pool
        abi = abis.V2_PAIR_ABI if pool_type == "v2" else abis.V3_POOL_ABI
        contract = w3.eth.contract(address=Web3.to_checksum_address(identifier), abi=abi)
        token0 = contract.functions.token0().call()
        token1 = contract.functions.token1().call()
        symbol0, decimals0 = fetch_token_info(w3, token0, chain)
        symbol1, decimals1 = fetch_token_info(w3, token1, chain)
        return cls(
            w3, chain, identifier,
            token0=token0, token1=token1,
            decimals0=decimals0, decimals1=decimals1,
            symbol0=symbol0, symbol1=symbol1,
        )

    if len(hex_part) == 64:
        if not chain.state_view:
            raise ValueError(
                f"{chain.name} icin StateView adresi tanimli degil - V4 pool okunamaz"
            )
        # A caller that already worked the key out - because the chain could not be
        # asked - hands it in rather than having it looked up again.
        currency0, currency1, fee, tick_spacing, hooks = pool_key or fetch_v4_pool_key(
            w3, chain, identifier, from_block
        )
        symbol0, decimals0 = fetch_token_info(w3, currency0, chain)
        symbol1, decimals1 = fetch_token_info(w3, currency1, chain)
        return V4Pool(
            w3, chain, identifier, fee, tick_spacing, hooks,
            token0=currency0, token1=currency1,
            decimals0=decimals0, decimals1=decimals1,
            symbol0=symbol0, symbol1=symbol1,
        )

    raise ValueError(
        f"Girilen deger ({len(hex_part)} hex karakter) ne pool adresine (40) "
        "ne de V4 pool id'ye (64) benziyor"
    )


POOL_CLASSES = {"v2": V2Pool, "v3": V3Pool, "v4": V4Pool}


def restore_pool(chain, metadata, w3=None):
    """
    Rebuild a pool from a saved metadata() dict, without asking the chain anything.

    Discovering a pool costs several sequential RPC calls - probe the version, read
    token0/token1, then symbol and decimals for each side. On a slow endpoint that is
    seconds of waiting before the first price appears. None of those answers change
    though, so once a pool is known there is no reason to look them up again.

        entry = pool.metadata()      # save this when the token is first added
        pool = restore_pool(chain, entry, w3=w3)

    Raises KeyError if the dict is missing fields, so a caller can fall back to
    load_pool() when an old registry predates this.
    """
    pool_type = metadata["pool_type"]
    cls = POOL_CLASSES.get(pool_type)
    if cls is None:
        raise ValueError(f"bilinmeyen pool tipi: {pool_type}")

    w3 = w3 or chain.connect()
    common = {
        "token0": metadata["token0"],
        "token1": metadata["token1"],
        "decimals0": metadata["decimals0"],
        "decimals1": metadata["decimals1"],
        "symbol0": metadata["symbol0"],
        "symbol1": metadata["symbol1"],
    }

    if pool_type == "v4":
        return V4Pool(w3, chain, metadata["identifier"], metadata["fee"],
                      metadata["tick_spacing"], metadata["hooks"], **common)
    if pool_type == "v3":
        # fee passed in, so V3Pool skips its own fee() call too.
        return V3Pool(w3, chain, metadata["identifier"], fee=metadata["fee"], **common)
    return V2Pool(w3, chain, metadata["identifier"], **common)
