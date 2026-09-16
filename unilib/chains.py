"""
Chain configuration - the only thing that actually differs between EVM chains.

Uniswap's protocol is identical everywhere; only the addresses change. Keeping
those as data (rather than hardcoded in each project) is what makes the same code
work on Robinhood Chain, HyperEVM, Base, Arbitrum and so on.

Forks (PancakeSwap, HyperSwap, Project X...) are still Uniswap-shaped, so they fit
here too - they just may use a different V2 fee or lack V4 entirely.
"""
from dataclasses import dataclass, field, replace

from web3 import Web3

# V4 and most routers use the zero address to mean "the chain's native coin"
# (ETH, HYPE, BNB...) as opposed to its wrapped ERC20 form.
NATIVE_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class ChainConfig:
    """
    Addresses and constants for one chain.

    Anything set to None means "this chain does not have it, or it has not been
    verified yet" - the library raises a clear error instead of guessing when a
    feature needs a missing address.
    """

    name: str
    chain_id: int
    rpc_url: str
    wrapped_native: str
    native_symbol: str = "ETH"

    # V4: single PoolManager for all pools; StateView is its read-only view helper.
    state_view: str | None = None

    # V4: PositionManager keeps every PoolKey it ever minted into, indexed by the
    # first 25 bytes of the pool id. That is the only way back from an id to the key
    # it was hashed from - short of scanning the chain for the Initialize event,
    # which most endpoints refuse.
    position_manager: str | None = None

    v3_router: str | None = None
    v2_router: str | None = None

    # from bytecode on first use. A deployment often ships BOTH routers, so this

    # QuoterV2: the contract built for asking "what would this swap return", exactly,
    # including fees and price impact. Simulating through the router works too, but
    # the quoter is the purpose-built tool and needs no balance or allowance.
    v3_quoter: str | None = None

    # V4 has no per-pool contract to call, so swapping goes through the Universal
    # Router and quoting through a dedicated V4 quoter. Permit2 is how the Universal
    # Router is given permission to move ERC20s - a plain approve is not enough.
    universal_router: str | None = None
    permit2: str | None = None
    v4_quoter: str | None = None

    # Multicall3 sits at the same address on every chain checked so far - Robinhood,
    # HyperEVM and Base all carry identical bytecode there - so this defaults rather
    # than being set per chain. Point it elsewhere, or to None, if a chain differs.
    multicall: str | None = "0xcA11bde05977b3631167028862bE2a173976CA11"

    # Uniswap V2 charges 0.3% (997/1000). Forks differ - PancakeSwap uses 0.25%.
    # A wrong value here silently skews every V2 quote, so it belongs with the chain.
    v2_fee_numerator: int = 997
    v2_fee_denominator: int = 1000

    # Tokens that count as the "base" side of a pair, beyond native and wrapped
    # native. Used to work out which side of a pool is the token being tracked.
    # Format: {"USDC": ("0x...", 6)}
    extra_base_tokens: dict = field(default_factory=dict)

    def __post_init__(self):
        # Address comparison bugs are silent and nasty, so normalise once here
        # rather than sprinkling .lower() through every call site.
        for attr in ("wrapped_native", "state_view", "position_manager",
                     "v3_router", "v2_router",
                     "v3_quoter", "universal_router", "permit2", "v4_quoter",
                     "multicall"):
            value = getattr(self, attr)
            if value is not None:
                object.__setattr__(self, attr, Web3.to_checksum_address(value))

    @property
    def base_tokens(self):
        """
        All tokens treated as the quote/base side, as {symbol: (address, decimals)}.

        Native and wrapped native are always included; a chain can add stablecoins
        or anything else it commonly pairs against.
        """
        tokens = {
            self.native_symbol: (NATIVE_ADDRESS, 18),
            f"W{self.native_symbol}": (self.wrapped_native, 18),
        }
        tokens.update(self.extra_base_tokens)
        return tokens

    def is_base_token(self, address):
        """True if the address is one of this chain's base/quote assets."""
        address = (address or "").lower()
        return any(known.lower() == address for known, _ in self.base_tokens.values())

    def base_token_name(self, address):
        """Symbol for a base token address, or None if it is not a base token."""
        address = (address or "").lower()
        for symbol, (known, _) in self.base_tokens.items():
            if known.lower() == address:
                return symbol
        return None

    def with_rpc(self, rpc_url):
        """
        Same chain, different endpoint.

        The bundled rpc_url is a public endpoint: fine to start with, but rate-limited
        and shared. Anyone running this seriously will want their own (Alchemy,
        QuickNode, a local node), and that belongs in their project - not in the
        library. Returns a copy, so the shared config stays untouched.

            CHAIN = CHAINS[999].with_rpc("https://...")
        """
        return replace(self, rpc_url=rpc_url)

    def connect(self, verify=True):
        """
        Build a Web3 instance for this chain.

        Verifying the chain id catches a whole class of confusing bugs early -
        pointing at the wrong RPC otherwise just returns plausible-looking data
        from the wrong chain.
        """
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        if verify:
            actual = w3.eth.chain_id
            if actual != self.chain_id:
                raise RuntimeError(
                    f"{self.name}: beklenen chain_id {self.chain_id}, gelen {actual}. "
                    "Yanlis RPC'ye mi baglaniliyor?"
                )

        # web3's validation middleware re-reads eth_chainId twice for every request
        # it passes through - two thirds of all traffic on a read-heavy workload, and
        # billed as three calls where one was made. The check it performs is the one
        # just done above, once, against config; and transactions carry an explicit
        # chainId rather than whatever the endpoint reports at signing time. Dropping
        # it costs nothing that is not already covered.
        try:
            w3.middleware_onion.remove("validation")
        except (KeyError, ValueError):
            pass                       # older web3 without that middleware

        return w3


ROBINHOOD = ChainConfig(
    name="Robinhood Chain",
    chain_id=4663,
    rpc_url="https://rpc.mainnet.chain.robinhood.com",
    wrapped_native="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
    native_symbol="ETH",
    # Official Uniswap deployment. Every address below was checked on-chain, not just
    # taken from the docs - which themselves warn that addresses no longer match
    # across chains.
    #
    # V4: PoolManager is 0x8366a39CC670B4001A1121B8F6A443A643e40951, reachable from
    # StateView.poolManager(), so it is not stored separately.
    state_view="0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
    position_manager="0x58daec3116aae6d93017baaea7749052e8a04fa7",
    v4_quoter="0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
    universal_router="0x8876789976decbfcbbbe364623c63652db8c0904",
    permit2="0x000000000022D473030F116dDEE9F6B43aC78BA3",
    # V3: SwapRouter02 interface (deadline outside the params struct).
    v3_router="0xcaf681a66d020601342297493863e78c959e5cb2",
    v3_quoter="0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7",
    # V2 is not an official Uniswap deployment here; this router was found by looking
    # at what successful swaps on a live V2 pool actually call, then verified against
    # the pool's factory and WETH.
    v2_router="0x89e5DB8B5aA49aA85AC63f691524311AEB649eba",
)

HYPEREVM = ChainConfig(
    name="HyperEVM",
    chain_id=999,
    # The official endpoint (rpc.hyperliquid.xyz/evm) caps at 100 requests/minute and
    # kept rate-limiting during normal use. Measured on the same simulation call:
    #   drpc 357ms | official 792ms | hyperlend 1182ms
    # Other working public endpoints if this one degrades:
    #   https://rpc.hyperlend.finance
    #   https://rpc.purroofgroup.com
    #   https://hyperliquid-json-rpc.stakely.io
    # For anything serious, point a project at its own endpoint via chain.with_rpc().
    rpc_url="https://hyperliquid.drpc.org",
    # WHYPE is a canonical immutable system contract, same code as WETH.
    wrapped_native="0x5555555555555555555555555555555555555555",
    native_symbol="HYPE",
    # HyperSwap is an independent Uniswap fork, not an official Uniswap deployment.
    # No V4 found here, so state_view stays None.
    #
    # HyperSwap ships both routers; SwapRouter02 is used, same interface as the
    # other chains here. SwapRouter01 (0x4E2960a8cd19B467b82d26D83fAcb0fAE26b094D)
    # exposes the older interface and is not used.
    v3_router="0x6d99e7f6747af2cdbb5164b6dd50e40d4fde1e77",
    v3_quoter="0x03A918028f22D9E1473B7959C927AD7425A45C7C",  # QuoterV2
    # v2_router: two candidate addresses were checked and neither held a usable V2
    # router (one had no contract at all), so it stays unset rather than wrong.
    # V2 price reading still works without it - only V2 swaps need a router.
)

BASE = ChainConfig(
    name="Base",
    chain_id=8453,
    # mainnet.base.org rate-limits aggressively; publicnode held up better under
    # repeated calls. Swap in a paid endpoint if polling gets heavy.
    rpc_url="https://base-rpc.publicnode.com",
    # OP-stack standard predeploy.
    wrapped_native="0x4200000000000000000000000000000000000006",
    native_symbol="ETH",
    # Official Uniswap deployment; StateView.poolManager() verified to return
    # 0x498581fF718922c3f8e6A244956aF099B2652b2b.
    state_view="0xa3c0c9b65bad0b08107aa264b0f3db444b867a71",
    position_manager="0x7c5f5a4bbd8fd63184577525326123b519429bdc",
    # Verified on-chain, not just read off the deployments page: each address holds
    # a contract and carries the selector it is used for.
    v4_quoter="0x0d5e0f971ed27fbff6c2837bf31316121532048d",
    universal_router="0x6ff5693b99212da76ad316178a184ab56d299b43",
    permit2="0x000000000022D473030F116dDEE9F6B43aC78BA3",
    # SwapRouter02 - deadline is not in the params struct here.
    v3_router="0x2626664c2603336E57B271c5C0b26F421741e481",
    v2_router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
    extra_base_tokens={
        "USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
    },
)

ARC = ChainConfig(
    name="Arc",
    chain_id=5042,
    rpc_url="https://rpc.mainnet.arc.io",
    # Circle's chain settles in USDC, and the coin paying for gas is the same balance
    # as the ERC-20 - not a wrapper over it. There is no deposit() or withdraw() on
    # this address, and nothing to wrap: the native view carries 18 decimals for gas
    # accounting while the ERC-20 view carries the 6 USDC actually has. Measured on a
    # live pool, whose balance reads 643,883,880,397,000,000,000,000 natively and
    # 643,883,880,397 through the token - the same money, a factor of 1e12 apart.
    #
    # That gap is why only tracking is wired up here so far. Reading a price goes
    # through the pool's own decimals and is unaffected; a swap hands one number to
    # both msg.value and the router, which those two views do not agree on.
    wrapped_native="0x3600000000000000000000000000000000000000",
    native_symbol="USDC",
    # 24,546 bytes - the same size as Robinhood's - carrying both execute selectors,
    # with the canonical Permit2 and the V3 factory below embedded in its bytecode.
    # That last part is what ties it to this deployment rather than some other one.
    universal_router="0x4fca4a51ab4f23a7447b3284fbd7d73289a89fb1",
    # Deployed at the canonical address here, and referenced by the router above.
    permit2="0x000000000022D473030F116dDEE9F6B43aC78BA3",
    # Found by scanning live blocks for calls carrying a router's selectors, then
    # confirmed the way the notes require: its factory() returns exactly the factory
    # read off the pool above. Not taken from any deployment page - Uniswap has not
    # published one for this chain.
    #
    # Its WETH9() points at a 53-byte stub where every call reverts. SwapRouter02
    # needs a WETH9 address to deploy and there is nothing here to wrap, so a
    # placeholder went in. The consequence is not cosmetic: the native-in path
    # through this router cannot work, and a trade here is an ordinary ERC-20 one -
    # approve the 6-decimal USDC, call exactInputSingle. Which also means the 1e12
    # gap between the two views of the coin never comes up, because msg.value is
    # never involved.
    v3_router="0x53bf6b0684ec7ef91e1387da3d1a1769bc5a6f77",
    # Still missing. Only multi-hop routes need it; a single pool is simulated
    # through the router above.
    v3_quoter=None,
    v2_router=None,
    # V4 may or may not be live here; nothing has been verified, so nothing is stated.
    state_view=None,
    position_manager=None,
    v4_quoter=None,
)

CHAINS = {
    ROBINHOOD.chain_id: ROBINHOOD,
    HYPEREVM.chain_id: HYPEREVM,
    BASE.chain_id: BASE,
    ARC.chain_id: ARC,
}


def get_chain(chain_id):
    """Look up a known chain by id, with a readable error when it is not registered."""
    if chain_id not in CHAINS:
        known = ", ".join(f"{cid} ({c.name})" for cid, c in CHAINS.items())
        raise KeyError(f"chain_id {chain_id} tanimli degil. Bilinenler: {known}")
    return CHAINS[chain_id]
