"""Sets up a virtual Uniswap v2 world with mocked tokens and pairs.

Make some random trades against Ethereum Tester to see our Ethereum wallet management logic works."""

import logging
import os
import datetime
from decimal import Decimal
from pathlib import Path
from typing import List

import pytest
from eth_account import Account
from eth_typing import HexAddress
from web3 import EthereumTesterProvider, Web3
from web3.contract import Contract

from eth_defi.hotwallet import HotWallet
from eth_defi.balances import fetch_erc20_balances_by_transfer_event, convert_balances_to_decimal
from eth_defi.token import create_token
from eth_defi.uniswap_v2.deployment import UniswapV2Deployment, deploy_trading_pair, deploy_uniswap_v2_like
from tradeexecutor.ethereum.hot_wallet_sync import EthereumHotWalletReserveSyncer
from tradeexecutor.ethereum.uniswap_v2_execution_v0 import UniswapV2ExecutionModelVersion0
from tradeexecutor.ethereum.uniswap_v2_live_pricing import uniswap_v2_live_pricing_factory
from tradeexecutor.ethereum.uniswap_v2_valuation import uniswap_v2_sell_valuation_factory
from tradeexecutor.ethereum.uniswap_v2_routing import UniswapV2SimpleRoutingModel
from tradeexecutor.ethereum.uniswap_v2_valuation_v0 import UniswapV2PoolValuationMethodV0
from tradeexecutor.ethereum.universe import create_exchange_universe, create_pair_universe
from tradeexecutor.state.state import State
from tradeexecutor.state.portfolio import Portfolio
from tradeexecutor.state.trade import TradeExecution
from tradeexecutor.state.identifier import AssetIdentifier, TradingPairIdentifier
from tradeexecutor.strategy.approval import UncheckedApprovalModel

from tradeexecutor.strategy.bootstrap import import_strategy_file
from tradeexecutor.strategy.description import StrategyExecutionDescription
from tradeexecutor.strategy.execution_context import ExecutionContext, ExecutionMode
from tradeexecutor.strategy.qstrader import HAS_QSTRADER
from tradeexecutor.strategy.runner import StrategyRunner
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse
from tradeexecutor.strategy.universe_model import StaticUniverseModel
from tradeexecutor.cli.log import setup_pytest_logging, setup_discord_logging
from tradeexecutor.utils.timer import timed_task
from tradingstrategy.candle import GroupedCandleUniverse
from tradingstrategy.chain import ChainId
from tradingstrategy.exchange import ExchangeUniverse
from tradingstrategy.liquidity import GroupedLiquidityUniverse
from tradingstrategy.pair import LegacyPairUniverse, PandasPairUniverse
from tradingstrategy.timebucket import TimeBucket
from tradingstrategy.universe import Universe


# https://docs.pytest.org/en/latest/how-to/skipping.html#skip-all-test-functions-of-a-class-or-module
pytestmark = pytest.mark.skipif(os.environ.get("TRADING_STRATEGY_API_KEY") is None or not HAS_QSTRADER, reason="Set TRADING_STRATEGY_API_KEY environment variable to run this test")


#: How much values we allow to drift.
#: A hack fix receiving different decimal values on Github CI than on a local
APPROX_REL = 0.05
APPROX_REL_DECIMAL = Decimal("0.1")


@pytest.fixture(scope="module")
def logger(request):
    """Setup test logger."""
    logger = setup_pytest_logging(request)
    return logger


@pytest.fixture
def tester_provider():
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return EthereumTesterProvider()


@pytest.fixture
def eth_tester(tester_provider):
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return tester_provider.ethereum_tester


@pytest.fixture
def web3(tester_provider):
    """Set up a local unit testing blockchain."""
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return Web3(tester_provider)


@pytest.fixture
def chain_id(web3) -> int:
    """The test chain id (67)."""
    return web3.eth.chain_id


@pytest.fixture()
def deployer(web3) -> HexAddress:
    """Deploy account.

    Do some account allocation for tests.
    """
    return web3.eth.accounts[0]


@pytest.fixture
def usdc_token(web3, deployer: HexAddress) -> Contract:
    """Create USDC with 10M supply."""
    token = create_token(web3, deployer, "Fake USDC coin", "USDC", 10_000_000 * 10**6, 6)
    return token


@pytest.fixture
def aave_token(web3, deployer: HexAddress) -> Contract:
    """Create AAVE with 10M supply."""
    token = create_token(web3, deployer, "Fake Aave coin", "AAVE", 10_000_000 * 10**18, 18)
    return token


@pytest.fixture()
def uniswap_v2(web3, deployer) -> UniswapV2Deployment:
    """Uniswap v2 deployment."""
    deployment = deploy_uniswap_v2_like(web3, deployer)
    return deployment


@pytest.fixture
def weth_token(uniswap_v2: UniswapV2Deployment) -> Contract:
    """Mock some assets"""
    return uniswap_v2.weth


@pytest.fixture
def asset_usdc(usdc_token, chain_id) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(chain_id, usdc_token.address, usdc_token.functions.symbol().call(), usdc_token.functions.decimals().call())


@pytest.fixture
def asset_weth(weth_token, chain_id) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(chain_id, weth_token.address, weth_token.functions.symbol().call(), weth_token.functions.decimals().call())


@pytest.fixture
def asset_aave(aave_token, chain_id) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(chain_id, aave_token.address, aave_token.functions.symbol().call(), aave_token.functions.decimals().call())


@pytest.fixture
def aave_usdc_uniswap_trading_pair(web3, deployer, uniswap_v2, aave_token, usdc_token) -> HexAddress:
    """AAVE-USDC pool with 200k liquidity."""
    pair_address = deploy_trading_pair(
        web3,
        deployer,
        uniswap_v2,
        aave_token,
        usdc_token,
        1000 * 10**18,  # 1000 AAVE liquidity
        200_000 * 10**6,  # 200k USDC liquidity
    )
    return pair_address


@pytest.fixture
def weth_usdc_uniswap_trading_pair(web3, deployer, uniswap_v2, weth_token, usdc_token) -> HexAddress:
    """AAVE-USDC pool with 1.7M liquidity."""
    pair_address = deploy_trading_pair(
        web3,
        deployer,
        uniswap_v2,
        weth_token,
        usdc_token,
        1000 * 10**18,  # 1000 ETH liquidity
        1_700_000 * 10**6,  # 1.7M USDC liquidity
    )
    return pair_address


@pytest.fixture
def weth_usdc_pair(uniswap_v2, weth_usdc_uniswap_trading_pair, asset_usdc, asset_weth) -> TradingPairIdentifier:
    """WETH-USDC pair representation in the trade executor domain."""
    return TradingPairIdentifier(
        asset_weth,
        asset_usdc,
        weth_usdc_uniswap_trading_pair,
        exchange_address=uniswap_v2.factory.address,
        fee=0,
        internal_id=int(weth_usdc_uniswap_trading_pair, 16))


@pytest.fixture
def aave_usdc_pair(uniswap_v2, aave_usdc_uniswap_trading_pair, asset_usdc, asset_aave) -> TradingPairIdentifier:
    """AAVE-USDC pair representation in the trade executor domain."""
    return TradingPairIdentifier(
        asset_aave,
        asset_usdc,
        aave_usdc_uniswap_trading_pair,
        internal_id=int(aave_usdc_uniswap_trading_pair, 16),
        exchange_address=uniswap_v2.factory.address,
        fee=0,
    )


@pytest.fixture
def supported_reserves(usdc) -> List[AssetIdentifier]:
    """What reserve currencies we support for the strategy."""
    return [usdc]


@pytest.fixture()
def hot_wallet(web3: Web3, usdc_token: Contract, deployer: HexAddress) -> HotWallet:
    """Our trading Ethereum account.

    Start with 10,000 USDC cash and 2 ETH.
    """
    account = Account.create()
    web3.eth.send_transaction({"from": deployer, "to": account.address, "value": 2*10**18})
    usdc_token.functions.transfer(account.address, 10_000 * 10**6).transact({"from": deployer})
    wallet = HotWallet(account)
    wallet.sync_nonce(web3)
    return wallet


@pytest.fixture
def supported_reserves(asset_usdc) -> List[AssetIdentifier]:
    """The reserve currencies we support."""
    return [asset_usdc]


@pytest.fixture()
def portfolio() -> Portfolio:
    """A portfolio loaded with the initial cash.

    We start with 10,000 USDC.
    """
    portfolio = Portfolio()
    return portfolio


@pytest.fixture()
def state(portfolio) -> State:
    return State(portfolio=portfolio)


@pytest.fixture()
def exchange_universe(web3, uniswap_v2: UniswapV2Deployment) -> ExchangeUniverse:
    """We trade on one Uniswap v2 deployment on tester."""
    return create_exchange_universe(web3, [uniswap_v2])


@pytest.fixture()
def pair_universe(web3, exchange_universe: ExchangeUniverse, weth_usdc_pair, aave_usdc_pair) -> PandasPairUniverse:
    """We trade on two trading pairs."""
    exchange = next(iter(exchange_universe.exchanges.values()))  # Get the first exchange from the universe
    return create_pair_universe(web3, exchange, [weth_usdc_pair, aave_usdc_pair])


@pytest.fixture()
def universe(web3, exchange_universe: ExchangeUniverse, pair_universe: PandasPairUniverse) -> Universe:
    """Get our trading universe."""
    return Universe(
        time_bucket=TimeBucket.d1,
        chains=[ChainId(web3.eth.chain_id)],
        exchanges=list(exchange_universe.exchanges.values()),
        pairs=pair_universe,
        candles=GroupedCandleUniverse.create_empty_qstrader(),
        liquidity=GroupedLiquidityUniverse.create_empty(),
    )


@pytest.fixture()
def universe_model(universe, supported_reserves) -> StaticUniverseModel:
    """Model the trading universe for the trade executor."""
    return StaticUniverseModel(TradingStrategyUniverse(
        universe=universe,
        reserve_assets=supported_reserves
    ))


@pytest.fixture()
def strategy_path() -> Path:
    """Where do we load our strategy file."""
    return Path(os.path.join(os.path.dirname(__file__), "../strategies/test_only", "simulated_uniswap.py"))


@pytest.fixture()
def valuation_model_factory():
    """Revalue trading positions based on direct Uniswap v2 data."""
    return uniswap_v2_sell_valuation_factory


@pytest.fixture()
def routing_model(uniswap_v2, asset_usdc, asset_weth, weth_usdc_pair) -> UniswapV2SimpleRoutingModel:

    # Allowed exchanges as factory -> router pairs
    factory_router_map = {
        uniswap_v2.factory.address: (uniswap_v2.router.address, uniswap_v2.init_code_hash),
    }

    # Three way ETH quoted trades are routed thru WETH/USDC pool
    allowed_intermediary_pairs = {
        asset_weth.address: weth_usdc_pair.pool_address
    }

    return UniswapV2SimpleRoutingModel(
        factory_router_map,
        allowed_intermediary_pairs,
        reserve_token_address=asset_usdc.address,
        trading_fee=0.030,
    )


@pytest.fixture()
def runner(
        uniswap_v2,
        strategy_path,
        web3,
        hot_wallet,
        persistent_test_client,
        universe_model,
        valuation_model_factory,
        routing_model,
) -> StrategyRunner:
    """Construct the strategy runner."""

    strategy_factory = import_strategy_file(strategy_path)
    approval_model = UncheckedApprovalModel()
    execution_model = UniswapV2ExecutionModelVersion0(uniswap_v2, hot_wallet, confirmation_block_count=0)
    sync_method = EthereumHotWalletReserveSyncer(web3, hot_wallet.address)

    execution_context = ExecutionContext(ExecutionMode.unit_testing_trading)

    run_description: StrategyExecutionDescription = strategy_factory(
        execution_model=execution_model,
        timed_task_context_manager=timed_task,
        sync_method=sync_method,
        valuation_model_factory=valuation_model_factory,
        pricing_model_factory=uniswap_v2_live_pricing_factory,
        routing_model=routing_model,
        approval_model=approval_model,
        client=persistent_test_client,
        universe_model=universe_model,
        cash_buffer=0.05,
        execution_context=execution_context,
    )

    # Deconstruct strategy input
    runner: StrategyRunner = run_description.runner

    return runner


def test_simulated_uniswap_qstrader_strategy_single_trade(
        logger: logging.Logger,
        strategy_path: Path,
        web3: Web3,
        hot_wallet: HotWallet,
        uniswap_v2: UniswapV2Deployment,
        universe_model: StaticUniverseModel,
        valuation_model_factory,
        state: State,
        supported_reserves,
        weth_usdc_pair,
        weth_token,
        usdc_token,
        runner,
    ):
    """Tests a strategy that runs against a simulated Uniswap environment.

    Do a single trade and analyse data structures look correct after the trade.
    This trade wil buy 9500 USD worth of ETH and leave 500 USD in reserves.
    """

    # Run the trading over for the first day
    ts = datetime.datetime(2020, 1, 1)

    executor_universe: TradingStrategyUniverse = universe_model.universe
    universe = executor_universe.universe

    runner.pretick_check(ts, executor_universe)

    # Asset identifies used in testing
    exchange = universe.exchanges[0]
    weth_usdc = universe.pairs.get_one_pair_from_pandas_universe(exchange.exchange_id, "WETH", "USDC")
    aave_usdc = universe.pairs.get_one_pair_from_pandas_universe(exchange.exchange_id, "AAVE", "USDC")

    assert weth_usdc
    assert aave_usdc

    # see strategy/simulated_uniswap.py for different days we can have 0, 1, 2

    #
    # 1st day
    #

    debug_details = {}

    # We start with day_kind 1 that is all ETH day.
    debug_details = runner.tick(ts, executor_universe, state, debug_details)
    assert debug_details["day_kind"] == 1

    # We first check we got our 10,000 USDC deposit from hot_wallet fixture above
    # See StrategyRunner.sync_portfolio()
    assert len(debug_details["reserve_update_events"]) == 1
    assert debug_details["total_equity_at_start"] == 10_000
    assert debug_details["total_cash_at_start"] == 10_000
    assert debug_details["cash_buffer_percentage"] == 0.05

    # Check that the strategy thinking is 100% ETH
    # This comes from qstrader/portfolio_construction_model
    assert debug_details["alpha_model_weights"] == {weth_usdc.pair_id: 1}
    assert debug_details["target_prices"] == {weth_usdc.pair_id: pytest.approx(1705.12, rel=APPROX_REL)}
    assert debug_details["target_portfolio"] == {weth_usdc.pair_id: {"quantity": pytest.approx(Decimal('5.571455381439429643'), rel=APPROX_REL_DECIMAL)}}

    # The strategy should use all of our available USDC to buy ETH.
    assert len(debug_details["rebalance_trades"]) == 1

    # Check the executed portfolio balances
    assert state.portfolio.get_total_equity() == pytest.approx(9947.390072492823, rel=APPROX_REL)
    assert state.portfolio.get_current_cash() == pytest.approx(500, rel=APPROX_REL)

    # Check the open position
    assert len(state.portfolio.open_positions) == 1
    position = state.portfolio.get_open_position_for_pair(weth_usdc_pair)
    assert position is not None
    assert position.get_quantity() == pytest.approx(Decimal('5.54060129052079779'), rel=APPROX_REL_DECIMAL)
    assert position.get_value() == pytest.approx(9447.390072492823, rel=APPROX_REL)
    assert len(position.trades) == 1

    # Check the recorded trade history
    trades = list(state.portfolio.get_all_trades())
    assert len(trades) == 1
    t = trades[0]

    assert t.is_success()
    tx_info = t.blockchain_transactions[-1]
    assert tx_info.chain_id == 61   # Ethereum Tester
    assert tx_info.tx_hash.startswith("0x")
    assert tx_info.nonce == 1

    # Test raw Ethereum tx data access
    tx_dict = tx_info.get_transaction()
    assert tx_dict["chainId"] == 61
    assert tx_dict["value"] == 0
    assert tx_dict["v"] in (0, 1)
    assert tx_dict["s"] > 0
    assert tx_dict["r"] > 0

    # Check the raw on-chain token balances
    raw_balances = fetch_erc20_balances_by_transfer_event(web3, hot_wallet.address)
    balances = convert_balances_to_decimal(web3, raw_balances)
    assert balances[weth_token.address].value == pytest.approx(Decimal('5.54060129052079779'), rel=APPROX_REL_DECIMAL)
    assert balances[usdc_token.address].value == pytest.approx(Decimal('500'), rel=APPROX_REL_DECIMAL)

    # Portfolio value stays approx. the same after revaluation
    # There is some decrease, because now we value in the slippage we would get on Uniswap v2
    ts = datetime.datetime(2020, 1, 1, 12, 00)

    revaluation_method = UniswapV2PoolValuationMethodV0(uniswap_v2)
    state.revalue_positions(ts, revaluation_method)
    position = state.portfolio.get_open_position_for_pair(weth_usdc_pair)
    assert position.last_pricing_at == ts
    assert position.last_reserve_price == 1
    assert position.last_token_price == pytest.approx(1704.3998300611074, rel=APPROX_REL)
    assert state.portfolio.get_total_equity() == pytest.approx(9943.399898000001, rel=APPROX_REL)
    assert state.portfolio.get_current_cash() == pytest.approx(500, rel=APPROX_REL)


def test_simulated_uniswap_qstrader_strategy_one_rebalance(
        logger: logging.Logger,
        strategy_path: Path,
        web3: Web3,
        hot_wallet: HotWallet,
        uniswap_v2: UniswapV2Deployment,
        universe: Universe,
        state: State,
        supported_reserves,
        weth_usdc_pair,
        aave_usdc_pair,
        aave_token,
        weth_token,
        usdc_token,
        universe_model,
        runner,
    ):
    """Tests a strategy that runs against a simulated Uniswap environment.

    Cycles 100% ETH > 100% AAVE through USDC on the second day.
    """

    executor_universe: TradingStrategyUniverse = universe_model.universe
    universe = executor_universe.universe

    # Asset identifies used in testing
    exchange = universe.exchanges[0]
    weth_usdc = universe.pairs.get_one_pair_from_pandas_universe(exchange.exchange_id, "WETH", "USDC")
    aave_usdc = universe.pairs.get_one_pair_from_pandas_universe(exchange.exchange_id, "AAVE", "USDC")

    # Run the trading for the first 3 days starting on arbitrarily chosen date 1-1-2020
    debug_details = {}
    runner.tick(datetime.datetime(2020, 1, 1), executor_universe, state, debug_details)
    runner.tick(datetime.datetime(2020, 1, 2), executor_universe, state, debug_details)

    #
    # After Day #2 - we rebalance be 100% ETH -> 100% AAVE
    #

    assert debug_details["positions_at_start_of_construction"] == {
        weth_usdc.pair_id: {'quantity': pytest.approx(Decimal('5.54060129052079779'), rel=APPROX_REL_DECIMAL)},
    }

    assert debug_details["target_portfolio"] == {
        aave_usdc.pair_id: {"quantity": pytest.approx(Decimal('47.087532545984750242'), rel=APPROX_REL_DECIMAL)},
        weth_usdc.pair_id:  {"quantity": 0},
    }

    # Sell 100% ETH, buy 100% AAVE
    # Because Aave pool is small (200k USD) we get a lot of slippage 47 -> 44 AAVE on execution
    # TODO: This will be fixed with a better estimator
    trades: List[TradeExecution] = debug_details["rebalance_trades"]
    assert len(trades) == 2
    assert trades[0].is_sell()
    assert trades[1].is_buy()
    assert trades[1].planned_quantity == pytest.approx(Decimal('47.087532545984750242'), rel=APPROX_REL_DECIMAL)
    assert trades[1].executed_quantity == pytest.approx(Decimal('44.971760338523757841'), rel=APPROX_REL_DECIMAL)

    # Check the raw on-chain token balances
    raw_balances = fetch_erc20_balances_by_transfer_event(web3, hot_wallet.address)
    balances = convert_balances_to_decimal(web3, raw_balances)
    assert balances[weth_token.address].value == pytest.approx(Decimal('0'), rel=APPROX_REL_DECIMAL)
    assert balances[aave_token.address].value == pytest.approx(Decimal('44.971760338523757841'), rel=APPROX_REL_DECIMAL)
    assert balances[usdc_token.address].value == pytest.approx(Decimal('497.169995'), rel=APPROX_REL_DECIMAL)


def test_simulated_uniswap_qstrader_strategy_round_trip(
        logger: logging.Logger,
        strategy_path: Path,
        web3: Web3,
        hot_wallet: HotWallet,
        uniswap_v2: UniswapV2Deployment,
        universe: Universe,
        state: State,
        supported_reserves,
        weth_usdc_pair,
        aave_usdc_pair,
        weth_token,
        usdc_token,
        aave_token,
        universe_model,
        runner,
    ):
    """Tests a strategy that runs against a simulated Uniswap environment.

    Cycle to the 50% ETH / 50% AAVE positions through rebalance.
    """

    executor_universe = universe_model.universe
    universe = executor_universe.universe

    # Asset identifies used in testing
    exchange = universe.exchanges[0]
    weth_usdc = universe.pairs.get_one_pair_from_pandas_universe(exchange.exchange_id, "WETH", "USDC")
    aave_usdc = universe.pairs.get_one_pair_from_pandas_universe(exchange.exchange_id, "AAVE", "USDC")

    # Run the trading for the first 3 days starting on arbitrarily chosen date 1-1-2020
    debug_details = {}
    runner.tick(datetime.datetime(2020, 1, 1), executor_universe, state, {})
    runner.tick(datetime.datetime(2020, 1, 2), executor_universe, state, {})
    runner.tick(datetime.datetime(2020, 1, 3), executor_universe, state, debug_details)

    #
    # After Day #3 - we should be 50%/50% ETH/AAVE
    #

    assert debug_details["target_portfolio"] == {
        weth_usdc.pair_id: {"quantity": pytest.approx(Decimal('2.754805368333548720'), rel=APPROX_REL_DECIMAL)},
        aave_usdc.pair_id: {"quantity": pytest.approx(Decimal('21.354907569100333830'), rel=APPROX_REL_DECIMAL)},
    }

    assert debug_details["normalised_weights"] == {
        weth_usdc.pair_id: 0.5,
        aave_usdc.pair_id: 0.5,
    }

    # We have lost some money in trading fees
    assert state.portfolio.get_total_equity() == pytest.approx(9983.773698830146, rel=APPROX_REL)
    assert state.portfolio.get_current_cash() == pytest.approx(913.1489764500003, rel=APPROX_REL)

    # Check our two open positions
    assert len(state.portfolio.open_positions) == 2
    position_1 = state.portfolio.get_open_position_for_pair(weth_usdc_pair)
    assert position_1.get_quantity() == pytest.approx(Decimal('2.754699344146152536'), rel=APPROX_REL_DECIMAL)
    assert position_1.get_value() == pytest.approx(4684.555636069401, rel=APPROX_REL)
    position_2 = state.portfolio.get_open_position_for_pair(aave_usdc_pair)
    assert position_2.get_value() == pytest.approx(4362.366674108017, rel=APPROX_REL)
    assert position_2.get_quantity() == pytest.approx(Decimal('21.354907569100333830'), rel=APPROX_REL_DECIMAL)

    # Check the raw on-chain token balances
    raw_balances = fetch_erc20_balances_by_transfer_event(web3, hot_wallet.address)
    balances = convert_balances_to_decimal(web3, raw_balances)
    assert balances[weth_token.address].value == pytest.approx(Decimal('2.747249930253346052'), rel=APPROX_REL_DECIMAL)
    assert balances[aave_token.address].value == pytest.approx(Decimal('21.354907569100333830'), rel=APPROX_REL_DECIMAL)

    # The cash balance should be ~500 USD but due to huge AAVE price estimation error it is not
    assert balances[usdc_token.address].value == pytest.approx(Decimal('936.529351'), rel=APPROX_REL_DECIMAL)


