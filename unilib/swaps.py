"""
Swap execution - buying and selling through a chain's routers.

Deliberately kept apart from pools.py: reading prices needs no wallet, and a price
tracker should never have to touch a private key. Only this module does.

Buying and selling are separate methods rather than one swap(direction=...) call,
because they genuinely differ:

              buy (base -> token)        sell (token -> base)
  input       native coin as msg.value   an ERC20
  approve     not needed                 required
  output      an ERC20                   wrapped native, unless unwrapped
  V2 function swapExactETHForTokens      swapExactTokensForETH

Forcing those into one function would just move the branching inside it.
"""
import time
from dataclasses import dataclass

from web3 import Web3

from . import abis
from .chains import NATIVE_ADDRESS

MAX_UINT256 = 2**256 - 1
# Permit2 stores allowances in narrower slots than a normal ERC20 approval.
MAX_UINT160 = 2**160 - 1
MAX_UINT48 = 2**48 - 1
DEFAULT_DEADLINE_SECONDS = 120
DEFAULT_SLIPPAGE_PCT = 0.5


@dataclass
class TxResult:
    """
    Outcome of an attempted transaction.

    Carries the failure reason instead of raising, so a monitoring loop can log it
    and retry rather than crashing mid-run.
    """

    success: bool
    tx_hash: str | None = None
    error: str | None = None
    amount_out: float | None = None

    def __bool__(self):
        return self.success


class Swapper:
    """
    Sends swaps for one chain with one wallet.

    `account` is an eth_account LocalAccount. How it is stored (keyring, env, a
    hardware wallet) is the caller's business - this library never reads keys itself.
    """

    def __init__(self, chain, account, w3=None):
        self.chain = chain
        self.account = account
        self.w3 = w3 or chain.connect()
        self._codec_cache = None

    @property
    def address(self):
        return self.account.address

    # -- routers ------------------------------------------------------------

    def _v3_router(self):
        if not self.chain.v3_router:
            raise ValueError(f"{self.chain.name} icin v3_router adresi tanimli degil")
        return self.w3.eth.contract(address=self.chain.v3_router, abi=abis.V3_ROUTER02_ABI)

    def _exact_input_params(self, token_in, token_out, fee, recipient, amount_in_wei,
                            min_out_wei, deadline):
        """
        Params tuple for exactInputSingle.

        SwapRouter02 keeps the deadline out of the struct - it belongs to the
        multicall that wraps the call, not to the swap itself. `deadline` is taken
        here anyway so callers do not have to know that.
        """
        return (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
            recipient,
            amount_in_wei,
            min_out_wei,
            0,
        )

    def _v2_router(self):
        if not self.chain.v2_router:
            raise ValueError(f"{self.chain.name} icin v2_router adresi tanimli degil")
        return self.w3.eth.contract(address=self.chain.v2_router, abi=abis.V2_ROUTER_ABI)

    def _router_for(self, pool):
        """Router contract for this pool's version. Raises if the chain has none configured."""
        if pool.pool_type == "v3":
            return self._v3_router()
        if pool.pool_type == "v2":
            return self._v2_router()
        return None  # V4 goes through the Universal Router, handled separately

    def _codec(self):
        """
        Encoder for Universal Router calldata.

        V4 pools have no router of their own: swapping means sending the Universal
        Router a list of commands plus their ABI-encoded inputs. That encoding is
        fiddly and fails silently when wrong, so it is delegated to a library built
        for it rather than hand-rolled here.
        """
        if self._codec_cache is None:
            try:
                from uniswap_universal_router_decoder import RouterCodec
            except ImportError:
                raise ImportError(
                    "V4 swaps need the uniswap-universal-router-decoder package: "
                    "pip install uniswap-universal-router-decoder"
                )
            self._codec_cache = RouterCodec(w3=self.w3)
        return self._codec_cache

    def _v4_pool_key(self, pool):
        """pool.pool_key as the codec's own PoolKey object."""
        currency0, currency1, fee, tick_spacing, hooks = pool.pool_key
        return self._codec().encode.v4_pool_key(currency0, currency1, fee, tick_spacing, hooks)

    def _require_universal_router(self):
        if not self.chain.universal_router:
            raise ValueError(
                f"{self.chain.name} icin universal_router adresi tanimli degil - "
                "V4 swap yapilamaz"
            )
        return Web3.to_checksum_address(self.chain.universal_router)

    # -- allowance ----------------------------------------------------------

    def allowance(self, token_address, spender):
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address), abi=abis.ERC20_ABI
        )
        return token.functions.allowance(self.address, Web3.to_checksum_address(spender)).call()

    def ensure_allowance(self, token_address, amount_wei, spender, unlimited=True):
        """
        Approve the router to spend a token, if it cannot already.

        Defaults to an unlimited approval so this costs one transaction ever, rather
        than one before every sell. The trade-off is that the router keeps standing
        permission to move that token - acceptable for a router whose address you
        have verified, but pass unlimited=False to approve only this trade's amount.

        Returns a TxResult; already-approved is a success with no tx_hash.
        """
        spender = Web3.to_checksum_address(spender)
        if self.allowance(token_address, spender) >= amount_wei:
            return TxResult(success=True)

        try:
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_address), abi=abis.ERC20_ABI
            )
            amount = MAX_UINT256 if unlimited else amount_wei
            tx = token.functions.approve(spender, amount).build_transaction({
                "from": self.address,
                "nonce": self.w3.eth.get_transaction_count(self.address),
            })
            receipt = self._sign_and_send(tx)
            return TxResult(success=receipt.status == 1, tx_hash=receipt.transactionHash.hex())
        except Exception as e:
            return TxResult(success=False, error=f"approve basarisiz: {e}")

    def permit2_allowance(self, token_address, spender):
        """(amount, expiration, nonce) that Permit2 currently grants this spender."""
        permit2 = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.chain.permit2), abi=abis.PERMIT2_ABI
        )
        return permit2.functions.allowance(
            self.address,
            Web3.to_checksum_address(token_address),
            Web3.to_checksum_address(spender),
        ).call()

    def ensure_permit2_allowance(self, token_address, amount_wei, spender):
        """
        Give the Universal Router permission to move a token, the V4 way.

        Permission is two-layered and both layers must be in place:

          1. the token is approved to Permit2 (a normal ERC20 approve)
          2. Permit2 is told that this spender may move up to N until some expiry

        Each layer costs a transaction the first time and nothing afterwards, since
        both are set to their maximum. A plain ERC20 approve to the router alone does
        not work - the router never calls transferFrom itself.

        Returns a TxResult; already-permitted is a success with no tx_hash.
        """
        if not self.chain.permit2:
            return TxResult(success=False,
                            error=f"{self.chain.name} icin permit2 adresi tanimli degil")

        permit2_address = Web3.to_checksum_address(self.chain.permit2)
        try:
            # Layer 1: token -> Permit2
            approval = self.ensure_allowance(token_address, amount_wei, permit2_address)
            if not approval:
                return approval

            # Layer 2: Permit2 -> spender
            allowed, expiration, _nonce = self.permit2_allowance(token_address, spender)
            if allowed >= amount_wei and expiration > time.time():
                return TxResult(success=True, tx_hash=approval.tx_hash)

            permit2 = self.w3.eth.contract(address=permit2_address, abi=abis.PERMIT2_ABI)
            tx = permit2.functions.approve(
                Web3.to_checksum_address(token_address),
                Web3.to_checksum_address(spender),
                MAX_UINT160,
                MAX_UINT48,
            ).build_transaction(self._tx_params())
            receipt = self._sign_and_send(tx)
            return TxResult(success=receipt.status == 1,
                            tx_hash=receipt.transactionHash.hex())
        except Exception as e:
            return TxResult(success=False, error=f"permit2 approve basarisiz: {e}")

    # -- simulation ---------------------------------------------------------

    def simulate(self, pool, token_in, amount_in=None, amount_in_wei=None):
        """
        Ask the chain what this swap would actually return, without sending anything.

        This is an eth_call: the node runs the real router code against current state
        and hands back the result, costing nothing and changing nothing. Unlike a spot
        price it accounts for fees, price impact and any transfer tax - which is what
        makes it the right basis for a minimum-output figure.

        Pass amount_in_wei to say exactly how much, in the token's own units. Prefer it
        whenever the number came from the chain (a balance, for instance): converting
        wei to a float and back does not round-trip, and the result can land slightly
        ABOVE the balance - which the router then rejects.

        Note for sells: the router does a transferFrom, so an allowance must already
        exist or the simulation reverts. Approve first, then simulate.

        Returns the output amount in human units, or None if the call reverted.
        """
        is_native_in = token_in.lower() in (NATIVE_ADDRESS, self.chain.wrapped_native.lower())
        decimals_in = pool.decimals0 if token_in.lower() == pool.token0.lower() else pool.decimals1
        decimals_out = pool.decimals1 if token_in.lower() == pool.token0.lower() else pool.decimals0
        amount_in_wei = _resolve_wei(amount_in, amount_in_wei, decimals_in)

        try:
            if pool.pool_type == "v3":
                router = self._v3_router()
                token_out = pool.token1 if token_in.lower() == pool.token0.lower() else pool.token0
                params = self._exact_input_params(
                    self._as_wrapped(token_in),
                    self._as_wrapped(token_out),
                    pool.fee,
                    self.address,
                    amount_in_wei,
                    0,
                    int(time.time()) + DEFAULT_DEADLINE_SECONDS,
                )
                out_wei = self._call_with_balance(
                    router.functions.exactInputSingle(params),
                    value=amount_in_wei if is_native_in else 0,
                )
                return out_wei / 10**decimals_out

            if pool.pool_type == "v2":
                # V2's own getAmountsOut is a view function and already exact for
                # ordinary tokens. It cannot see a transfer tax, so treat its answer
                # as an upper bound on fee-on-transfer tokens.
                router = self._v2_router()
                token_out = pool.token1 if token_in.lower() == pool.token0.lower() else pool.token0
                path = [
                    Web3.to_checksum_address(self._as_wrapped(token_in)),
                    Web3.to_checksum_address(self._as_wrapped(token_out)),
                ]
                amounts = router.functions.getAmountsOut(amount_in_wei, path).call()
                return amounts[-1] / 10**decimals_out

            if pool.pool_type == "v4":
                # V4 has no router to simulate against - the pool lives inside the
                # PoolManager. The dedicated quoter runs the real swap instead, hook
                # included, which matters here: a hooked pool can charge its own cut
                # while reporting fee=0.
                return pool.quote_exact(token_in, amount_in_wei / 10**decimals_in)

            raise ValueError(f"bilinmeyen pool tipi: {pool.pool_type}")
        except ValueError:
            raise
        except Exception:
            return None

    def _call_with_balance(self, fn, value=0):
        """
        eth_call that does not fail merely because the wallet is short of funds.

        A value-bearing call is balance-checked by the node, so simulating a buy from
        an empty or underfunded wallet errors out before the router logic ever runs -
        which would make it impossible to ask "what would I get for 5 HYPE?" without
        already holding 5 HYPE. state_override tells the node to pretend the balance
        is there for this call only; nothing is sent and no state changes.

        Not every RPC implements state_override, so this falls back to a plain call.
        """
        tx = {"from": self.address, "value": value}
        if value:
            headroom = value * 2 + 10**18  # cover the value plus gas
            override = {self.address: {"balance": hex(headroom)}}
            try:
                return fn.call(tx, state_override=override)
            except (TypeError, ValueError) as e:
                # TypeError: older web3 without the parameter.
                # ValueError: RPC rejecting the override (unsupported method).
                if "state_override" not in str(e) and not isinstance(e, TypeError):
                    raise
        return fn.call(tx)

    def _as_wrapped(self, address):
        """Routers speak in wrapped-native terms; the zero address only means native as msg.value."""
        if address.lower() == NATIVE_ADDRESS:
            return self.chain.wrapped_native
        return address

    def _min_out(self, pool, token_in, amount_in, slippage_pct, min_out, amount_in_wei=None):
        """
        Work out the minimum acceptable output.

        Prefers a simulation, because a spot-price estimate ignores price impact and
        so produces a floor the trade cannot actually meet - which is exactly how a
        swap ends up reverting with "Too little received".
        """
        if min_out is not None:
            return min_out, None
        expected = self.simulate(pool, token_in, amount_in, amount_in_wei)
        if expected is None:
            # Simulation unavailable (no router configured, or it reverted) - fall
            # back to the pool's own quote, which is exact on V2 and an estimate on V3.
            decimals = pool.decimals0 if token_in.lower() == pool.token0.lower() else pool.decimals1
            human = amount_in if amount_in is not None else amount_in_wei / 10**decimals
            expected = pool.quote(token_in, human)
        return expected * (1 - slippage_pct / 100), expected

    # -- buying -------------------------------------------------------------

    def buy(self, pool, amount_in, slippage_pct=DEFAULT_SLIPPAGE_PCT, min_out=None,
            fee_on_transfer=False, deadline_seconds=DEFAULT_DEADLINE_SECONDS):
        """
        Spend the chain's native coin to buy the pool's tracked token.

        No approval is involved: native coin travels as msg.value and the router
        wraps it, so there is no ERC20 for anyone to need permission over.

        Set fee_on_transfer=True for tokens that take a cut on transfer ("vergili"
        tokens) - on V2 those revert through the plain function. It is harmless on
        ordinary tokens too, just slightly more gas.
        """
        # Resolved up front, outside the try: a missing router is a configuration
        # problem, not a failed trade, so it should raise rather than come back as a
        # retryable TxResult that no amount of retrying will fix.
        router = self._router_for(pool)
        if pool.pool_type == "v4":
            self._require_universal_router()

        # V4 prices in the native coin itself (currency zero-address), the other
        # versions in its wrapped form.
        token_in = pool.base if pool.pool_type == "v4" else self.chain.wrapped_native
        min_out_human, expected = self._min_out(pool, token_in, amount_in, slippage_pct, min_out)

        amount_in_wei = int(amount_in * 10**18)
        min_out_wei = int(min_out_human * 10**pool.decimals)
        deadline = int(time.time()) + deadline_seconds

        try:
            if pool.pool_type == "v4":
                tx = self._v4_swap_tx(
                    pool, token_in, amount_in_wei, min_out_wei, deadline, native_value=True
                )
            elif pool.pool_type == "v3":
                params = self._exact_input_params(
                    self.chain.wrapped_native, pool.token, pool.fee,
                    self.address, amount_in_wei, min_out_wei, deadline,
                )
                swap_fn = router.functions.exactInputSingle(params)
                tx = self._v3_tx(router, swap_fn, deadline, value=amount_in_wei)
            elif pool.pool_type == "v2":
                path = [
                    Web3.to_checksum_address(self.chain.wrapped_native),
                    Web3.to_checksum_address(pool.token),
                ]
                fn = (
                    router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens
                    if fee_on_transfer
                    else router.functions.swapExactETHForTokens
                )
                tx = fn(min_out_wei, path, self.address, deadline).build_transaction(
                    self._tx_params(value=amount_in_wei)
                )
            else:
                raise ValueError(f"bilinmeyen pool tipi: {pool.pool_type}")

            receipt = self._sign_and_send(tx)
            return TxResult(
                success=receipt.status == 1,
                tx_hash=receipt.transactionHash.hex(),
                amount_out=expected,
            )
        except (ValueError, ImportError):
            # Configuration problems (missing router, missing package) are permanent -
            # let them raise instead of coming back as a retryable failure.
            raise
        except Exception as e:
            return TxResult(success=False, error=str(e))

    # -- selling ------------------------------------------------------------

    def sell(self, pool, amount_in=None, slippage_pct=DEFAULT_SLIPPAGE_PCT, min_out=None,
             fee_on_transfer=False, unwrap=True, deadline_seconds=DEFAULT_DEADLINE_SECONDS,
             approve=True, unlimited_approve=True, amount_in_wei=None):
        """
        Sell the pool's tracked token back into the chain's native coin.

        Approval is handled first (unless approve=False), because the router has to
        pull the tokens out of the wallet - and because a simulation cannot run
        without it either.

        unwrap=True returns native coin rather than the wrapped ERC20. On V2 this is
        free: swapExactTokensForETH already unwraps. On V3 the router must be told to,
        which is done by batching an unwrapWETH9 call after the swap - otherwise the
        proceeds arrive as wrapped tokens.

        For "sell everything", pass the balance as amount_in_wei rather than a float.
        balanceOf returns an integer; dividing it to a float and multiplying back does
        not always return the same number, and when it lands high the router cannot
        pull that much and reverts with STF.
        """
        # Resolved before anything else so a missing router raises here rather than
        # after an approval has already been sent.
        router = self._router_for(pool)
        amount_in_wei = _resolve_wei(amount_in, amount_in_wei, pool.decimals)

        if approve:
            if pool.pool_type == "v4":
                # V4 sells go through the Universal Router, which moves tokens via
                # Permit2 rather than pulling them itself - a plain approve is not
                # enough and would leave the swap reverting.
                approval = self.ensure_permit2_allowance(
                    pool.token, amount_in_wei, self._require_universal_router()
                )
            else:
                approval = self.ensure_allowance(
                    pool.token, amount_in_wei, router.address, unlimited=unlimited_approve
                )
            if not approval:
                return approval

        min_out_human, expected = self._min_out(
            pool, pool.token, None, slippage_pct, min_out, amount_in_wei=amount_in_wei
        )
        min_out_wei = int(min_out_human * 10**pool.base_decimals)
        deadline = int(time.time()) + deadline_seconds

        try:
            if pool.pool_type == "v4":
                # Usually no unwrap step: in V4 the native coin is a currency in
                # its own right, so the proceeds arrive native already. The exception
                # is a pool holding the wrapped form, which _v4_swap_tx unwraps.
                tx = self._v4_swap_tx(
                    pool, pool.token, amount_in_wei, min_out_wei, deadline,
                    native_value=False, unwrap=unwrap,
                )
            elif pool.pool_type == "v3":
                # When unwrapping, proceeds must land on the router first so it has
                # something to unwrap, then unwrapWETH9 forwards the native coin on.
                recipient = router.address if unwrap else self.address
                params = self._exact_input_params(
                    pool.token, self.chain.wrapped_native, pool.fee,
                    recipient, amount_in_wei, min_out_wei, deadline,
                )
                swap_fn = router.functions.exactInputSingle(params)
                extra = None
                if unwrap:
                    extra = router.functions.unwrapWETH9(min_out_wei, self.address)
                tx = self._v3_tx(router, swap_fn, deadline, extra=extra)

            elif pool.pool_type == "v2":
                path = [
                    Web3.to_checksum_address(pool.token),
                    Web3.to_checksum_address(self.chain.wrapped_native),
                ]
                fn = (
                    router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens
                    if fee_on_transfer
                    else router.functions.swapExactTokensForETH
                )
                tx = fn(
                    amount_in_wei, min_out_wei, path, self.address, deadline
                ).build_transaction(self._tx_params())
            else:
                raise ValueError(f"bilinmeyen pool tipi: {pool.pool_type}")

            receipt = self._sign_and_send(tx)
            return TxResult(
                success=receipt.status == 1,
                tx_hash=receipt.transactionHash.hex(),
                amount_out=expected,
            )
        except (ValueError, ImportError):
            # Configuration problems (missing router, missing package) are permanent -
            # let them raise instead of coming back as a retryable failure.
            raise
        except Exception as e:
            return TxResult(success=False, error=str(e))

    # -- routes -------------------------------------------------------------

    def _mixed_route_tx(self, route, amount_in_wei, min_out_wei, deadline):
        """
        Build a route that crosses Uniswap versions as one Universal Router call.

        The router runs a command list, so versions can be mixed - but each pair of
        neighbouring hops has to hand the intermediate token over, and each version
        does that differently:

          V3/V2 hops send their output to the router itself rather than the wallet,
          and a hop after the first spends what the router already holds
          (`..._from_balance`), so no amount has to be predicted.

          V4 hops settle into the PoolManager first and then swap the resulting
          credit. `CONTRACT_BALANCE` on the settle means "whatever arrived", and
          `OPEN_DELTA` on the swap means "whatever that credit is" - which is what
          keeps the handoff exact. Settling everything and then swapping a fixed
          amount leaves an unresolved balance, and V4 rejects the whole call for it.

        The last hop is the only one that gets a floor, and the only one that pays out
        to the wallet. Putting a floor on an intermediate would be guarding a number
        nobody ever holds.
        """
        from uniswap_universal_router_decoder import FunctionRecipient, V4Constants

        codec = self._codec()
        chain = codec.encode.chain()
        wrapped = Web3.to_checksum_address(self.chain.wrapped_native)
        native_in = route.currency_in.lower() == NATIVE_ADDRESS
        last = len(route.pools) - 1

        # A route whose last hop is V2 or V3 pays out the wrapped coin, because that
        # is what those pools hold. Unwrapping is one more command and costs nothing,
        # and leaving the wrapped form in the wallet would be an extra step the user
        # has to undo by hand every time they sell.
        unwrap_at_end = (route.token.lower() == wrapped.lower()
                         and route.pools[-1].pool_type != "v4")

        # V3 and V2 have no concept of the native coin. If the route starts there and
        # its first hop is one of those, the coin is wrapped into the router first.
        if native_in and route.pools[0].pool_type != "v4":
            chain = chain.wrap_eth(FunctionRecipient.ROUTER, amount_in_wei)

        for i, (pool, currency_in, currency_out) in enumerate(
                zip(route.pools, route.currencies[:-1], route.currencies[1:])):
            is_last = i == last
            token_in = wrapped if currency_in.lower() == NATIVE_ADDRESS else currency_in
            token_out = wrapped if currency_out.lower() == NATIVE_ADDRESS else currency_out
            floor = min_out_wei if is_last else 0
            # Proceeds that still need unwrapping stay with the router for one more
            # command rather than going to the wallet wrapped.
            to_wallet = is_last and not unwrap_at_end
            recipient = FunctionRecipient.SENDER if to_wallet else FunctionRecipient.ROUTER

            if pool.pool_type == "v3":
                path = [Web3.to_checksum_address(token_in), pool.fee,
                        Web3.to_checksum_address(token_out)]
                if i == 0 and not native_in:
                    # First hop with an ERC20 input: the router pulls from the wallet
                    # through Permit2.
                    chain = chain.v3_swap_exact_in(recipient, amount_in_wei, floor, path,
                                                   payer_is_sender=True)
                elif i == 0:
                    # The wrapped coin is already sitting in the router.
                    chain = chain.v3_swap_exact_in(recipient, amount_in_wei, floor, path,
                                                   payer_is_sender=False)
                else:
                    chain = chain.v3_swap_exact_in_from_balance(recipient, floor, path)

            elif pool.pool_type == "v2":
                path = [Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out)]
                if i == 0:
                    chain = chain.v2_swap_exact_in(recipient, amount_in_wei, floor, path,
                                                   payer_is_sender=not native_in)
                else:
                    chain = chain.v2_swap_exact_in_from_balance(recipient, floor, path)

            elif pool.pool_type == "v4":
                v4 = chain.v4_swap()
                if i == 0:
                    v4 = v4.settle(Web3.to_checksum_address(currency_in),
                                   amount_in_wei, True)
                else:
                    v4 = v4.settle(Web3.to_checksum_address(currency_in),
                                   V4Constants.CONTRACT_BALANCE.value, False)
                v4 = v4.swap_exact_in_single(
                    pool_key=self._v4_pool_key(pool),
                    zero_for_one=currency_in.lower() == pool.token0.lower(),
                    amount_in=V4Constants.OPEN_DELTA.value,
                    amount_out_min=floor,
                )
                if to_wallet:
                    v4 = v4.take_all(Web3.to_checksum_address(currency_out), floor)
                else:
                    # Leave it with the router so the next hop can spend it.
                    v4 = v4.take(Web3.to_checksum_address(currency_out),
                                 Web3.to_checksum_address(
                                     "0x0000000000000000000000000000000000000002"),
                                 V4Constants.OPEN_DELTA.value)
                chain = v4.build_v4_swap()
            else:
                raise ValueError(f"bilinmeyen pool tipi: {pool.pool_type}")

        if unwrap_at_end:
            chain = chain.unwrap_weth(FunctionRecipient.SENDER, min_out_wei)

        return chain.build_transaction(
            self.address,
            amount_in_wei if native_in else 0,
            ur_address=self._require_universal_router(),
            deadline=deadline,
            chain_id=self.chain.chain_id,
        )

    def swap_route(self, route, amount_in=None, slippage_pct=DEFAULT_SLIPPAGE_PCT,
                   min_out=None, approve=True, unlimited_approve=True,
                   deadline_seconds=DEFAULT_DEADLINE_SECONDS, amount_in_wei=None):
        """
        Send a whole route as one transaction.

        Every hop settles inside the PoolManager, so the intermediate currency never
        reaches the wallet: no approval for it, no balance in it, and nothing left
        stranded if a later hop fails. The alternative - one transaction per hop - can
        strand you holding the middle token when the second leg moves, and on this
        chain that middle token may have no liquid way out at all.

        min_out applies to the final output only. There is no useful floor to put on a
        hop whose proceeds you never hold; what matters is what arrives at the end.

        Approval is needed only when the input is an ERC20, and only through Permit2,
        since the Universal Router moves tokens that way rather than pulling them
        itself. A native input travels as msg.value and needs nothing.

        Pass amount_in_wei when the figure came from the chain - a balance being sold
        in full, typically. Converting it to a float and back does not round-trip, and
        landing a single wei above the balance is enough for the transfer to fail.
        """
        ur_address = self._require_universal_router()
        codec = self._codec()

        currency_in = Web3.to_checksum_address(route.currency_in)
        currency_out = Web3.to_checksum_address(route.token)
        is_native_in = currency_in.lower() == NATIVE_ADDRESS

        amount_in_wei = _resolve_wei(amount_in, amount_in_wei, route.base_decimals)
        amount_in_human = amount_in_wei / 10**route.base_decimals

        # The quoted figure is the floor's basis: it already carries the fees, the
        # hooks and the price impact of this exact path at this exact size, none of
        # which a per-pool estimate would see.
        if min_out is None:
            expected = route.quote(amount_in_human)
            if expected is None:
                return TxResult(success=False,
                                error="rota fiyat vermiyor - bir adim calismiyor olabilir")
            min_out_human = expected * (1 - slippage_pct / 100)
        else:
            expected, min_out_human = None, min_out
        min_out_wei = int(min_out_human * 10**route.decimals)

        if approve and not is_native_in:
            approval = self.ensure_permit2_allowance(currency_in, amount_in_wei, ur_address)
            if not approval:
                return approval

        deadline = int(time.time()) + deadline_seconds

        try:
            if not route.is_v4_only:
                tx = self._mixed_route_tx(route, amount_in_wei, min_out_wei, deadline)
                receipt = self._sign_and_send(tx)
                return TxResult(
                    success=receipt.status == 1,
                    tx_hash=receipt.transactionHash.hex(),
                    amount_out=expected,
                )

            tx = (
                codec.encode.chain()
                .v4_swap()
                .swap_exact_in(
                    currency_in=currency_in,
                    path_keys=route.path_keys(),
                    amount_in=amount_in_wei,
                    amount_out_min=min_out_wei,
                )
                .settle_all(currency_in, amount_in_wei)
                .take_all(currency_out, min_out_wei)
                .build_v4_swap()
                .build_transaction(
                    self.address,
                    amount_in_wei if is_native_in else 0,
                    ur_address=ur_address,
                    deadline=deadline,
                    chain_id=self.chain.chain_id,
                )
            )
            receipt = self._sign_and_send(tx)
            return TxResult(
                success=receipt.status == 1,
                tx_hash=receipt.transactionHash.hex(),
                amount_out=expected,
            )
        except (ValueError, ImportError):
            # Configuration problems are permanent - a missing router or a route with a
            # non-V4 hop will not start working on the next attempt.
            raise
        except Exception as e:
            return TxResult(success=False, error=str(e))

    # -- plumbing -----------------------------------------------------------

    def _v4_swap_tx(self, pool, token_in, amount_in_wei, min_out_wei, deadline,
                    native_value=False, unwrap=False):
        """
        Build a V4 swap as a Universal Router transaction.

        V4 settles in deltas rather than transfers: the swap records what is owed and
        what is due, then SETTLE_ALL pays the input side and TAKE_ALL collects the
        output. All three go in one command list, executed atomically.

        native_value=True sends the input as msg.value - the buy case, where the input
        is the chain's native coin and there is nothing to approve. Selling instead
        needs the router to already hold permission through Permit2.

        Not every V4 pool prices in the native coin, though. A few hold the wrapped
        form instead, and there the input is an ERC20 like any other: settling it
        against the user pulls it through Permit2, which reverts with
        AllowanceExpired unless the wallet both holds wrapped coin and has granted
        that allowance - while msg.value would be sent on top and left behind.
        Wrapping inside the call removes both problems. WRAP_ETH turns the msg.value
        into wrapped coin held by the router, and settling CONTRACT_BALANCE spends
        that rather than anything of the user's, so no approval exists to expire.
        The swap then takes OPEN_DELTA: the settled amount is only known while the
        call runs, and naming a fixed figure leaves an unresolved balance that V4
        rejects outright.

        Selling into such a pool is the mirror image - the proceeds arrive wrapped,
        so they are taken to the router and unwrapped before they reach the wallet.
        """
        from uniswap_universal_router_decoder import FunctionRecipient, V4Constants

        ur_address = self._require_universal_router()
        codec = self._codec()
        wrapped = Web3.to_checksum_address(self.chain.wrapped_native)

        token_out = pool.token1 if token_in.lower() == pool.token0.lower() else pool.token0
        zero_for_one = token_in.lower() == pool.token0.lower()
        pays_wrapped = native_value and token_in.lower() == wrapped.lower()
        takes_wrapped = unwrap and token_out.lower() == wrapped.lower()

        chain = codec.encode.chain()
        if pays_wrapped:
            chain = chain.wrap_eth(FunctionRecipient.ROUTER, amount_in_wei)

        v4 = chain.v4_swap()
        if pays_wrapped:
            v4 = v4.settle(wrapped, V4Constants.CONTRACT_BALANCE.value, False)
        v4 = v4.swap_exact_in_single(
            pool_key=self._v4_pool_key(pool),
            zero_for_one=zero_for_one,
            amount_in=V4Constants.OPEN_DELTA.value if pays_wrapped else amount_in_wei,
            amount_out_min=min_out_wei,
        )
        if not pays_wrapped:
            v4 = v4.settle_all(Web3.to_checksum_address(token_in), amount_in_wei)
        if takes_wrapped:
            # Held by the router for one more command rather than going to the wallet
            # wrapped. ADDRESS_THIS is the router's own address inside a command list.
            v4 = v4.take(wrapped,
                         Web3.to_checksum_address(
                             "0x0000000000000000000000000000000000000002"),
                         V4Constants.OPEN_DELTA.value)
        else:
            v4 = v4.take_all(Web3.to_checksum_address(token_out), min_out_wei)

        chain = v4.build_v4_swap()
        if takes_wrapped:
            chain = chain.unwrap_weth(FunctionRecipient.SENDER, min_out_wei)

        return chain.build_transaction(
            self.address,
            amount_in_wei if native_value else 0,
            ur_address=ur_address,
            deadline=deadline,
            chain_id=self.chain.chain_id,
        )

    def _v3_tx(self, router, swap_fn, deadline, extra=None, value=0):
        """
        Build the V3 transaction.

        SwapRouter02's params carry no deadline, so every call goes through
        multicall(deadline, data) - which is also where a follow-up call attaches,
        such as unwrapping the proceeds after a sell.

        The caller just says "this swap, plus optionally this follow-up".
        """
        calls = [swap_fn]
        if extra is not None:
            calls.append(extra)

        data = [Web3.to_bytes(hexstr=c._encode_transaction_data()) for c in calls]
        fn = router.functions.multicall(deadline, data)

        return fn.build_transaction(self._tx_params(value=value))

    def _tx_params(self, value=0):
        # chainId is stated rather than asked for. web3 would otherwise fetch it while
        # building, and a signed transaction should carry the chain this library was
        # configured for - not whichever one the endpoint claims at that moment.
        params = {
            "from": self.address,
            "chainId": self.chain.chain_id,
            "nonce": self.w3.eth.get_transaction_count(self.address),
        }
        if value:
            params["value"] = value
        return params

    def _sign_and_send(self, tx):
        """
        Sign, broadcast, wait for the receipt.

        Gas is left to web3's estimate on purpose: estimation runs the transaction
        against current state first, so a trade that would revert fails here - before
        it is broadcast and before any gas is spent on it.
        """
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(tx_hash)


def _resolve_wei(amount_in, amount_in_wei, decimals):
    """
    Settle on an integer amount from whichever form the caller supplied.

    Exact wei wins when both are given. The float path is a convenience for
    hand-typed amounts; anything that came out of the chain should travel as wei,
    because float64 cannot hold an 18-decimal balance exactly and the round trip can
    end up above the real number.
    """
    if amount_in_wei is not None:
        return int(amount_in_wei)
    if amount_in is None:
        raise ValueError("amount_in ya da amount_in_wei verilmeli")
    return int(amount_in * 10**decimals)

