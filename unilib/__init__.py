"""
unilib - Uniswap V2/V3/V4 pool reading across EVM chains.

The protocol is the same everywhere; only addresses change. Point it at a chain,
hand it a pool address or a V4 pool id, and ask for prices without caring which
version you are talking to.

    from unilib import CHAINS, load_pool

    chain = CHAINS[999]                        # HyperEVM
    w3 = chain.connect()

    pool = load_pool(chain, "0x314cAdD6...", w3=w3)
    print(pool.symbol, pool.price())           # HYPURR, price in WHYPE
    print(pool.quote_sell(1000))               # selling 1000 HYPURR -> ? WHYPE
"""
from .chains import CHAINS, HYPEREVM, NATIVE_ADDRESS, ROBINHOOD, ChainConfig, get_chain
from .pools import (
    find_pool_key,
    Pool,
    V2Pool,
    V3Pool,
    V4Pool,
    detect_pool_type,
    fetch_token_info,
    fetch_v4_pool_key,
    get_pool_manager,
    load_pool,
    restore_pool,
)
from .multicall import aggregate, fetch_prices, quote_many
from .routes import Route
from .swaps import Swapper, TxResult

__version__ = "0.1.0"

__all__ = [
    "CHAINS",
    "ChainConfig",
    "HYPEREVM",
    "NATIVE_ADDRESS",
    "ROBINHOOD",
    "get_chain",
    "Pool",
    "V2Pool",
    "V3Pool",
    "V4Pool",
    "detect_pool_type",
    "fetch_token_info",
    "fetch_v4_pool_key",
    "get_pool_manager",
    "load_pool",
    "restore_pool",
    "Route",
    "aggregate",
    "fetch_prices",
    "quote_many",
    "Swapper",
    "TxResult",
    "pricing",
]

from . import pricing  # noqa: E402  (re-exported for unilib.pricing.* access)
