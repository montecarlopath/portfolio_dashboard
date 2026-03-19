from datetime import date
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field


BrokerModeType = Literal["preview", "dry_run", "submit"]
BrokerEnvironmentType = Literal["paper", "live"]

OrderIntentType = Literal[
    "open_debit_spread",
    "close_debit_spread",
    "roll_spread",
    "migrate_hedge",
    "unknown",
]


MarketRegimeType = Literal[
    "strong_bull",
    "extended_bull",
    "early_breakdown",
    "high_crash_risk",
    "localized_bubble",
    "neutral",
]

HedgeAggressivenessType = Literal[
    "low",
    "medium",
    "high",
]

HedgeStyleType = Literal[
    "balanced",
    "cost_sensitive",
    "crash_paranoid",
    "correction_focused",
]

HedgeAssetType = Literal[
    "SPY",
    "QQQ",
    "hybrid",
]



class PortfolioBetaRow(BaseModel):
    symbol: str
    value: float
    weight: float
    beta: Optional[float] = None
    beta_adjusted_exposure: float




# --- Accounts ---
class AccountInfo(BaseModel):
    id: str
    credential_name: str
    account_type: str
    display_name: str
    status: str


# --- Manual Cash Flow ---
class ManualCashFlowRequest(BaseModel):
    account_id: str
    date: date
    type: str = "deposit"  # deposit / withdrawal
    amount: float
    description: str = ""


class ManualCashFlowResponse(BaseModel):
    status: str
    date: str
    type: str
    amount: float


class ManualCashFlowDeleteResponse(BaseModel):
    status: str
    deleted_id: int


# --- Sync ---
class SyncStatus(BaseModel):
    status: str  # idle / syncing / error
    last_sync_date: Optional[str] = None
    initial_backfill_done: bool = False
    message: str = ""


class SyncTriggerResponse(BaseModel):
    status: str
    synced_accounts: Optional[int] = None
    reason: Optional[str] = None


class SymphonyExportJobStatus(BaseModel):
    status: str  # idle / running / cancelling / complete / cancelled / error
    job_id: Optional[str] = None
    exported: int = 0
    processed: int = 0
    total: Optional[int] = None
    message: str = ""
    error: Optional[str] = None


class SymphonyExportConfig(BaseModel):
    enabled: bool = True
    local_path: str = ""


class AppConfigResponse(BaseModel):
    finnhub_api_key: Optional[str] = None
    finnhub_configured: bool
    polygon_configured: bool
    local_auth_token: str
    symphony_export: Optional[SymphonyExportConfig] = None
    screenshot: Optional[Dict[str, Any]] = None
    test_mode: bool
    first_start_test_mode: bool = False
    first_start_run_id: Optional[str] = None
    composer_config_ok: bool
    composer_config_error: Optional[str] = None


class SaveSymphonyExportResponse(BaseModel):
    ok: bool
    local_path: str
    enabled: bool


class SaveSymphonyExportRequest(BaseModel):
    local_path: str
    enabled: bool = True


class OkResponse(BaseModel):
    ok: bool


class ScreenshotUploadResponse(BaseModel):
    ok: bool
    path: str


class SaveScreenshotConfigRequest(BaseModel):
    local_path: str
    enabled: bool = True
    account_id: str = ""
    chart_mode: str = ""
    period: str = ""
    custom_start: str = ""
    hide_portfolio_value: bool = False
    metrics: List[str] = []
    benchmarks: List[str] = []


# --- Portfolio ---
class DailyPortfolioRow(BaseModel):
    date: date
    portfolio_value: float
    cash_balance: float
    net_deposits: float
    total_fees: float
    total_dividends: float


class DailyMetricsRow(BaseModel):
    date: date
    daily_return_pct: float
    cumulative_return_pct: float
    total_return_dollars: float
    cagr: float
    annualized_return: float
    annualized_return_cum: float
    time_weighted_return: float
    money_weighted_return: float
    win_rate: float
    num_wins: int
    num_losses: int
    avg_win_pct: float
    avg_loss_pct: float
    max_drawdown: float
    current_drawdown: float
    sharpe_ratio: float
    calmar_ratio: float
    sortino_ratio: float
    annualized_volatility: float
    best_day_pct: float
    worst_day_pct: float
    profit_factor: float


class PortfolioSummary(BaseModel):
    portfolio_value: float
    net_deposits: float
    total_return_dollars: float
    daily_return_pct: float
    cumulative_return_pct: float
    cagr: float
    annualized_return: float
    annualized_return_cum: float
    time_weighted_return: float
    money_weighted_return: float
    money_weighted_return_period: float
    sharpe_ratio: float
    calmar_ratio: float
    sortino_ratio: float
    max_drawdown: float
    max_drawdown_date: Optional[str] = None
    current_drawdown: float
    win_rate: float
    num_wins: int
    num_losses: int
    avg_win_pct: float
    avg_loss_pct: float
    annualized_volatility: float
    best_day_pct: float
    best_day_date: Optional[str] = None
    worst_day_pct: float
    worst_day_date: Optional[str] = None
    profit_factor: float
    median_drawdown: float = 0.0
    longest_drawdown_days: int = 0
    median_drawdown_days: int = 0
    total_fees: float
    total_dividends: float
    last_updated: Optional[str] = None


# --- Holdings ---
class HoldingSnapshot(BaseModel):
    symbol: str
    quantity: float
    market_value: float = 0.0
    allocation_pct: Optional[float] = None


class HoldingsForDate(BaseModel):
    date: date
    holdings: List[HoldingSnapshot]


class PortfolioHoldingsResponse(BaseModel):
    date: Optional[str] = None
    holdings: List[HoldingSnapshot]


class HoldingsHistoryRow(BaseModel):
    date: str
    num_positions: int


# --- Transactions ---
class TransactionRow(BaseModel):
    date: date
    symbol: str
    action: str
    quantity: float
    price: float
    total_amount: float
    account_id: Optional[str] = None
    account_name: Optional[str] = None


class TransactionListResponse(BaseModel):
    total: int
    transactions: List[TransactionRow]


# --- Cash Flows ---
class CashFlowRow(BaseModel):
    id: int
    date: date
    type: str
    amount: float
    description: str = ""
    is_manual: bool = False
    account_id: Optional[str] = None
    account_name: Optional[str] = None


# --- Performance chart data ---
class PerformancePoint(BaseModel):
    date: date
    portfolio_value: float
    net_deposits: float
    cumulative_return_pct: float
    daily_return_pct: float
    time_weighted_return: float
    money_weighted_return: float
    current_drawdown: float


# --- Benchmark history ---
class BenchmarkHistoryPoint(BaseModel):
    date: str
    close: float
    return_pct: float
    drawdown_pct: float
    mwr_pct: float


class BenchmarkHistoryResponse(BaseModel):
    ticker: str
    data: List[BenchmarkHistoryPoint]


class TradingSessionsResponse(BaseModel):
    exchange: str
    start_date: str
    end_date: str
    sessions: List[str]


class SymphonyBenchmarkResponse(BaseModel):
    name: str
    ticker: str
    data: List[BenchmarkHistoryPoint]


# --- Symphony list/catalog ---
class SymphonyHoldingRow(BaseModel):
    ticker: str
    allocation: float
    value: float
    last_percent_change: float


class SymphonyListRow(BaseModel):
    id: str
    position_id: str
    account_id: str
    account_name: str
    name: str
    color: str
    value: float
    net_deposits: float
    cash: float
    total_return: float
    cumulative_return_pct: float
    simple_return: float
    time_weighted_return: float
    last_dollar_change: float
    last_percent_change: float
    sharpe_ratio: float
    max_drawdown: float
    annualized_return: float
    invested_since: str
    last_rebalance_on: Optional[str] = None
    next_rebalance_on: Optional[str] = None
    rebalance_frequency: str
    holdings: List[SymphonyHoldingRow]


class SymphonyCatalogRow(BaseModel):
    symphony_id: str
    name: str
    source: str


# --- Symphony summary/backtest ---
class SymphonySummary(BaseModel):
    symphony_id: str
    account_id: str
    period: str
    start_date: str
    end_date: str
    portfolio_value: float
    net_deposits: float
    total_return_dollars: float
    daily_return_pct: float
    cumulative_return_pct: float
    cagr: float
    annualized_return: float
    annualized_return_cum: float
    time_weighted_return: float
    money_weighted_return: float
    money_weighted_return_period: float
    sharpe_ratio: float
    calmar_ratio: float
    sortino_ratio: float
    max_drawdown: float
    current_drawdown: float
    win_rate: float
    num_wins: int
    num_losses: int
    annualized_volatility: float
    best_day_pct: float
    worst_day_pct: float
    profit_factor: float


class SymphonyBacktestResponse(BaseModel):
    stats: Dict[str, Any]
    dvm_capital: Dict[str, Any]
    tdvm_weights: Dict[str, Any]
    benchmarks: Dict[str, Any]
    summary_metrics: Dict[str, Any]
    first_day: int
    last_market_day: int
    cached_at: str
    last_semantic_update_at: str = ""


# --- Trade preview ---
class TradePreviewRow(BaseModel):
    symphony_id: str
    symphony_name: str
    account_id: str
    account_name: str
    ticker: str
    notional: float
    quantity: float
    prev_value: float
    prev_weight: float
    next_weight: float
    side: str


class SymphonyTradeRecommendation(BaseModel):
    ticker: str
    name: Optional[str] = None
    side: str
    share_change: float
    cash_change: float
    average_price: float
    prev_value: float
    prev_weight: float
    next_weight: float


class SymphonyTradePreviewResponse(BaseModel):
    symphony_id: str
    symphony_name: str
    rebalanced: bool
    next_rebalance_after: str
    symphony_value: float
    recommended_trades: List[SymphonyTradeRecommendation]
    markets_closed: Optional[bool] = None

class PortfolioBetaRow(BaseModel):
    symbol: str
    value: float
    weight: float
    beta: Optional[float] = None
    beta_adjusted_exposure: float
    dollar_beta_exposure: float


class PortfolioBetaResponse(BaseModel):
    date: str
    benchmark: str
    portfolio_value: float
    portfolio_beta: float
    portfolio_dollar_beta: float
    rows: List[PortfolioBetaRow]



class RegimeSignalSnapshot(BaseModel):
    spy_above_50dma: bool
    spy_above_200dma: bool
    spy_distance_from_200dma_pct: float
    spy_rsi_14: float
    breadth_pct_above_200dma: float
    vix_level: float
    vix_term_structure_ratio: Optional[float] = None
    credit_stress_score: Optional[float] = None
    liquidity_stress_score: Optional[float] = None
    localized_bubble_score: Optional[float] = None


class MarketRegimeResponse(BaseModel):
    regime: MarketRegimeType
    market_risk_score: float = Field(..., ge=0.0, le=1.0)
    new_hedge_aggressiveness: HedgeAggressivenessType
    signals: RegimeSignalSnapshot
    reasons: List[str]


class PortfolioRiskMetricRow(BaseModel):
    name: str
    value: float
    label: str
    interpretation: str


class PortfolioRiskMetricsResponse(BaseModel):
    as_of_date: str
    benchmark: str

    portfolio_value: float

    portfolio_beta: float
    portfolio_dollar_beta: float

    portfolio_crash_beta: float
    portfolio_volatility_beta: float
    portfolio_liquidity_beta: float

    gross_long_exposure_pct: float
    gross_short_exposure_pct: float
    net_exposure_pct: float
    net_beta_exposure_pct: float

    metrics: List[PortfolioRiskMetricRow]

class HedgeSourceContribution(BaseModel):
    source: str
    positions_count: int = 0
    option_positions_count: int = 0
    symbols: List[str] = []

    structural_hedge_exposure_dollars: float = 0.0
    option_hedge_exposure_dollars: float = 0.0
    current_hedge_exposure_dollars: float = 0.0

    current_hedge_premium_cost: float = 0.0
    current_hedge_premium_market_value: float = 0.0
    current_hedge_premium_cost_basis: float = 0.0


class HedgeSourceBreakdown(BaseModel):
    composer: HedgeSourceContribution
    alpaca: HedgeSourceContribution


class HedgeIntelligenceResponse(BaseModel):
    as_of_date: str
    benchmark: str

    market_regime: MarketRegimeType
    market_risk_score: float = Field(..., ge=0.0, le=1.0)
    new_hedge_aggressiveness: HedgeAggressivenessType

    portfolio_value: float

    portfolio_beta: float
    portfolio_dollar_beta: float
    portfolio_crash_beta: float
    portfolio_volatility_beta: float
    portfolio_liquidity_beta: float

    gross_long_exposure_pct: float
    gross_short_exposure_pct: float
    net_exposure_pct: float
    net_beta_exposure_pct: float

    current_hedge_pct: float = Field(..., ge=0.0, le=1.0)
    recommended_hedge_pct: float = Field(..., ge=0.0, le=1.0)
    additional_hedge_pct: float = Field(..., ge=0.0, le=1.0)

    current_hedge_exposure_dollars: float
    recommended_hedge_exposure_dollars: float
    additional_hedge_exposure_dollars: float

    structural_hedge_exposure_dollars: float
    structural_hedge_exposure_pct: float = Field(..., ge=0.0, le=1.0)
    structural_hedge_capital_dollars: float
    structural_hedge_efficiency: float

    option_hedge_exposure_dollars: float
    option_hedge_exposure_pct: float = Field(..., ge=0.0, le=1.0)
    current_hedge_premium_cost: float
    current_hedge_premium_cost_pct: float = Field(..., ge=0.0, le=1.0)
    premium_hedge_efficiency: float
    current_hedge_premium_market_value: float
    current_hedge_premium_cost_basis: float

    hedge_unrealized_pnl: float
    hedge_cost_drag_dollars: float
    hedge_cost_drag_pct: float
    hedge_protection_capacity_dollars: float
    hedge_protection_capacity_pct: float
    hedge_marked_benefit_dollars: float
    hedge_marked_benefit_pct: float
    hedge_capacity_ratio: float
    hedged_beta_estimate: float
    unhedged_beta_estimate: float
    vix_level: float

    hedge_budget_pct: float = Field(..., ge=0.0, le=1.0)
    hedge_budget_dollars: float
    remaining_hedge_budget_dollars: float
    remaining_hedge_budget_pct: float = Field(..., ge=0.0, le=1.0)

    hedge_source_breakdown: HedgeSourceBreakdown

    reasons: List[str]
    insights: List[str]


class HedgeHistoryRow(BaseModel):
    date: str

    portfolio_value: float

    current_hedge_exposure_dollars: float
    current_hedge_pct: float

    structural_hedge_exposure_dollars: float
    option_hedge_exposure_dollars: float

    current_hedge_premium_market_value: float
    current_hedge_premium_cost_basis: float
    hedge_unrealized_pnl: float

    hedged_beta_estimate: float
    unhedged_beta_estimate: float


class HedgeHistoryResponse(BaseModel):
    as_of_date: str
    benchmark: str
    rows: List[HedgeHistoryRow]


class HedgeAttributionSummaryResponse(BaseModel):
    as_of_date: str
    benchmark: str

    hedge_pnl_ytd: float
    hedge_pnl_ytd_pct: float

    hedge_cost_drag_ytd: float
    hedge_cost_drag_ytd_pct: float

    hedge_benefit_on_down_days: float
    hedge_benefit_on_down_days_pct: float

    hedge_effectiveness_on_drawdowns: float

    average_hedge_capacity_dollars: float
    average_hedge_capacity_pct: float

    best_hedge_day: float
    worst_hedge_day: float

    days_analyzed: int

class OptionContractCandidate(BaseModel):
    symbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    delta: float | None = None
    bid: float | None = None
    ask: float | None = None
    mark: float | None = None
    open_interest: int | None = None
    volume: int | None = None
    score: float | None = None


class OptionSpreadSelection(BaseModel):
    structure_name: str
    underlying: str
    target_dte_min: int
    target_dte_max: int
    target_long_delta: float
    target_short_delta: float

    selected_expiry: str | None = None

    long_leg: OptionContractCandidate | None = None
    short_leg: OptionContractCandidate | None = None

    selection_score: float | None = None
    notes: List[str] = []


class HedgeSpreadSelectionResponse(BaseModel):
    as_of_date: str
    underlying: str
    market_regime: MarketRegimeType
    hedge_style: HedgeStyleType

    primary_spread: OptionSpreadSelection
    tail_spread: OptionSpreadSelection



class HedgeStructurePlan(BaseModel):
    structure_name: str
    selected_expiry: Optional[str] = None

    long_leg: Optional[OptionContractCandidate] = None
    short_leg: Optional[OptionContractCandidate] = None

    contracts: int = 0

    spread_width: float = 0.0
    debit_per_contract: float = 0.0
    max_payoff_per_contract: float = 0.0

    target_hedge_dollars: float = 0.0
    estimated_coverage_dollars: float = 0.0
    estimated_cost_dollars: float = 0.0

    coverage_to_cost_ratio: float = 0.0
    target_fill_pct: float = 0.0
    budget_used_pct: float = 0.0

    notes: List[str] = []



class HedgeExecutionPlanResponse(BaseModel):
    as_of_date: str
    benchmark: str

    hedge_style: HedgeStyleType
    hedge_asset: HedgeAssetType
    market_regime: MarketRegimeType

    primary_spread: HedgeStructurePlan
    tail_spread: HedgeStructurePlan

    total_estimated_cost_pct: float
    total_estimated_cost_dollars: float

    total_estimated_hedge_pct: float
    total_estimated_hedge_dollars: float


class HedgeRollDecision(BaseModel):
    action: Literal["hold", "add", "roll", "trim", "close"]
    structure_name: str
    reason: str


class HedgeRollEngineResponse(BaseModel):
    as_of_date: str
    benchmark: str
    hedge_style: HedgeStyleType
    hedge_asset: HedgeAssetType
    market_regime: MarketRegimeType

    current_hedge_pct: float
    recommended_hedge_pct: float
    additional_hedge_pct: float

    primary_decision: HedgeRollDecision
    tail_decision: HedgeRollDecision

    summary_action: Literal["hold", "add", "roll", "trim", "close"]
    notes: List[str] = []




class HedgePositionSnapshot(BaseModel):
    symbol: str
    quantity: float
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    market_value: float = 0.0
    total_cost_basis: float = 0.0
    delta_dollars: float = 0.0
    hedge_bucket: Literal["primary", "tail", "other"] = "other"
    structure_type: Literal["naked_put", "put_spread_leg", "tail_put", "other"] = "other"


class HedgeReconciliationAction(BaseModel):
    bucket: Literal["primary", "tail"]
    action: Literal[
        "hold_existing",
        "keep_partial",
        "add",
        "add_primary_spreads",
        "add_tail_spreads",
        "add_tail_spreads_now",
        "reduce",
        "replace",
        "replace_on_roll",
        "roll",
        "close",
    ]
    reason: str

    current_contracts_estimate: int = 0
    target_contracts: int = 0

    current_positions: List[str] = []

    current_exposure_dollars: float = 0.0
    target_exposure_dollars: float = 0.0
    exposure_gap_dollars: float = 0.0

    target_structure_name: str = ""
    target_expiry: Optional[str] = None
    target_long_symbol: Optional[str] = None
    target_short_symbol: Optional[str] = None


class HedgeExecutionPriorityItem(BaseModel):
    phase: Literal["immediate", "deferred", "roll"]
    bucket: Literal["primary", "tail"]
    action: Literal[
        "hold_existing",
        "keep_partial",
        "add",
        "add_primary_spreads",
        "add_tail_spreads",
        "add_tail_spreads_now",
        "reduce",
        "replace",
        "replace_on_roll",
        "roll",
        "close",
    ]
    priority: int
    reason: str


class HedgeReconciliationResponse(BaseModel):
    as_of_date: str
    benchmark: str
    hedge_style: HedgeStyleType
    hedge_asset: HedgeAssetType
    market_regime: MarketRegimeType

    current_positions: List[HedgePositionSnapshot]

    current_primary_exposure_dollars: float = 0.0
    current_tail_exposure_dollars: float = 0.0

    target_primary_exposure_dollars: float = 0.0
    target_tail_exposure_dollars: float = 0.0

    primary_action: HedgeReconciliationAction
    tail_action: HedgeReconciliationAction

    summary_action: Literal[
        "hold_existing",
        "keep_partial",
        "add",
        "add_primary_spreads",
        "add_tail_spreads",
        "add_tail_spreads_now",
        "reduce",
        "replace",
        "replace_on_roll",
        "roll",
        "close",
    ]

    execution_priority: List[HedgeExecutionPriorityItem] = []
    immediate_actions: List[str] = []
    deferred_actions: List[str] = []
    roll_actions: List[str] = []

    notes: List[str] = []

class HedgeStructureType(str):
    pass

class HedgeTradeLeg(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    option_type: str
    strike: float
    expiry: str


class HedgeTradeTicket(BaseModel):
    priority: int
    phase: Literal["immediate", "deferred", "roll"]
    bucket: Literal["primary", "tail"]

    action: Literal[
        "buy_spread",
        "buy_put",
        "sell_put",
        "hold",
        "hold_existing",
        "migrate_position",
        "close_profit_take",
        "close_regime_exit",
        "close_decay",
    ]

    description: str
    contracts: int = 0

    estimated_debit_dollars: float = 0.0
    estimated_max_payoff_dollars: float = 0.0
    estimated_coverage_added_dollars: float = 0.0

    budget_consumption_pct: float = 0.0
    coverage_to_cost_ratio: float = 0.0
    fits_remaining_budget: bool = True

    sizing_driver: Literal[
        "gap_constrained",
        "budget_constrained",
        "plan_capped",
        "exit_trigger",
        "none",
    ] = "none"

    unfilled_gap_dollars: float = 0.0
    post_ticket_gap_dollars: float = 0.0

    long_leg_symbol: Optional[str] = None
    short_leg_symbol: Optional[str] = None

    legs: List[HedgeTradeLeg] = []
    notes: List[str] = []


class HedgeTradeTicketResponse(BaseModel):
    as_of_date: str
    benchmark: str
    hedge_style: HedgeStyleType
    hedge_asset: HedgeAssetType
    market_regime: MarketRegimeType

    tickets: List[HedgeTradeTicket] = []

    total_estimated_debit_dollars: float = 0.0
    total_estimated_max_payoff_dollars: float = 0.0
    total_estimated_coverage_added_dollars: float = 0.0

    remaining_budget_before_tickets: float = 0.0
    remaining_budget_after_tickets: float = 0.0
    budget_fully_utilized: bool = False

    notes: List[str] = []



class BrokerOrderLeg(BaseModel):
    symbol: str
    side: str
    ratio_qty: int






class BrokerOrderLeg(BaseModel):
    symbol: str
    side: str
    ratio_qty: int


class BrokerValidationFlags(BaseModel):
    all_leg_symbols_present: bool
    positive_limit_price: bool
    valid_ratio_structure: bool
    executable_now: bool
    has_supported_order_class: bool
    broker_payload_complete: bool
    broker_precheck_passed: bool


class BrokerExecutionControls(BaseModel):
    limit_price_source: str = "ticket_estimated_debit"
    limit_price_buffer_pct: float = 0.0
    max_slippage_pct: float = 0.02
    preview_only: bool = True


class BrokerSubmissionResult(BaseModel):
    mode: str
    submitted: bool = False
    broker_order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: str = "not_submitted"
    message: str = ""


class BrokerOrderPayload(BaseModel):
    ticket_priority: int
    ticket_phase: str
    ticket_bucket: str
    ticket_action: str

    broker: str
    broker_environment: BrokerEnvironmentType = "paper"
    underlying: str

    order_class: str
    order_type: str
    time_in_force: str
    net_side: str
    order_intent: OrderIntentType = "unknown"

    client_order_id: Optional[str] = None

    limit_price: Optional[float] = None
    qty: Optional[int] = None

    estimated_debit_per_spread: float = 0.0
    estimated_debit_dollars: float = 0.0
    estimated_max_payoff_dollars: float = 0.0
    estimated_coverage_added_dollars: float = 0.0

    execution_controls: BrokerExecutionControls
    validation: BrokerValidationFlags

    legs: List[BrokerOrderLeg] = []
    alpaca_payload: Dict[str, Any] = {}

    notes: List[str] = []
    submission_result: Optional[BrokerSubmissionResult] = None


class BrokerOrderPayloadResponse(BaseModel):
    as_of_date: str
    benchmark: str
    hedge_style: str
    hedge_asset: str
    market_regime: str

    mode: BrokerModeType
    broker: str
    broker_environment: BrokerEnvironmentType = "paper"


    orders: List[BrokerOrderPayload]
    notes: List[str] = []


class HedgeInputSnapshot(BaseModel):
    as_of_date: str
    underlying: str
    market_regime: str
    hedge_style: str

    portfolio_value: float | None = None

    current_hedge_pct: float | None = None
    recommended_hedge_pct: float | None = None
    additional_hedge_pct: float | None = None
    remaining_hedge_budget_pct: float | None = None

    vix_level: float | None = None
    broker_environment: str | None = None



class BrokerOrderStatusLeg(BaseModel):
    symbol: str
    side: str | None = None
    ratio_qty: str | None = None
    position_intent: str | None = None


class BrokerOrderStatusRow(BaseModel):
    broker_order_id: str | None = None
    client_order_id: str | None = None
    status: str
    order_class: str | None = None
    order_type: str | None = None
    time_in_force: str | None = None
    limit_price: float | None = None
    qty: float | None = None
    filled_qty: float | None = None
    avg_fill_price: float | None = None
    submitted_at: str | None = None
    filled_at: str | None = None
    canceled_at: str | None = None
    expired_at: str | None = None
    failed_at: str | None = None
    legs: List[BrokerOrderStatusLeg] = []
    raw_status: Dict[str, Any] = {}
    notes: List[str] = []


class BrokerOrderStatusResponse(BaseModel):
    broker: str
    broker_environment: str | None = None
    queried_open_only: bool = True
    orders: List[BrokerOrderStatusRow]
    notes: List[str] = []

class BrokerCancelResponse(BaseModel):
    broker: str
    broker_environment: str | None = None
    canceled: bool
    broker_order_id: str | None = None
    client_order_id: str | None = None
    status: str
    message: str
    raw_response: Dict[str, Any] = {}


class BrokerPositionRow(BaseModel):
    broker: str
    broker_environment: str | None = None
    symbol: str
    asset_class: str | None = None
    exchange: str | None = None

    qty: float | None = None
    side: str | None = None

    market_value: float | None = None
    cost_basis: float | None = None
    avg_entry_price: float | None = None

    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None
    unrealized_intraday_pl: float | None = None
    unrealized_intraday_plpc: float | None = None

    current_price: float | None = None
    lastday_price: float | None = None
    change_today: float | None = None

    raw_position: Dict[str, Any] = {}
    notes: List[str] = []


class BrokerPositionsResponse(BaseModel):
    broker: str
    broker_environment: str | None = None
    positions: List[BrokerPositionRow]
    notes: List[str] = []

class UnifiedHoldingRow(BaseModel):
    source: str
    source_account_id: str | None = None
    source_account_name: str | None = None

    broker: str | None = None
    broker_environment: str | None = None

    symbol: str
    underlying: str | None = None
    asset_class: str | None = None
    position_type: str  # equity | option

    quantity: float | None = None
    side: str | None = None

    market_value: float | None = None
    allocation_pct: float | None = None

    avg_cost_basis: float | None = None
    total_cost_basis: float | None = None

    current_price: float | None = None
    delta_dollars: float | None = None

    option_type: str | None = None
    strike: float | None = None
    expiry: str | None = None

    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None

    raw_data: Dict[str, Any] = {}
    notes: List[str] = []


class UnifiedHoldingsResponse(BaseModel):
    as_of_date: str | None = None
    rows: List[UnifiedHoldingRow]
    notes: List[str] = []


from pydantic import BaseModel, Field
from typing import List


# ══════════════════════════════════════════════════════════════════════════════
# PART 1 — New schemas to add to app/schemas.py
#
# Paste these after the BrokerCancelResponse class (around line 1130).
# ══════════════════════════════════════════════════════════════════════════════
 
# ── Reprice event (nested inside HedgeOrderHistoryRow) ────────────────────────
class RepriceEvent(BaseModel):
    repriced_at_utc: str
    old_limit_price: float
    new_limit_price: float
    new_client_order_id: str
 
 
# ── One row in the order history table ────────────────────────────────────────
class HedgeOrderHistoryRow(BaseModel):
    # Identity
    client_order_id: str
    broker_order_id: Optional[str] = None
    broker: str = "alpaca"
    broker_environment: str = "paper"
    underlying: Optional[str] = None
    ticket_bucket: Optional[str] = None    # "primary" or "tail"
    ticket_action: Optional[str] = None
 
    # Submission
    mode: str
    submitted_at_utc: Optional[str] = None
    submission_status: Optional[str] = None
 
    # Lifecycle
    lifecycle_state: str                   # submitted/open/filled/cancelled/expired/failed/replaced
    last_checked_utc: Optional[str] = None
 
    # Fill details
    filled_at_utc: Optional[str] = None
    avg_fill_price: Optional[float] = None
    filled_qty: Optional[float] = None
 
    # Financial: estimated vs actual
    qty: Optional[int] = None
    estimated_debit_dollars: Optional[float] = None
    estimated_coverage_dollars: Optional[float] = None
    actual_debit_dollars: Optional[float] = None
 
    # Reprice history
    reprice_count: int = 0
    reprice_history: List[RepriceEvent] = []
    replaced_by_client_order_id: Optional[str] = None
 
 
# ── Response for GET /hedge/orders/history ────────────────────────────────────
class HedgeOrderHistoryResponse(BaseModel):
    as_of_date: str
    total_orders: int
    filled: int
    open: int
    cancelled: int
    expired: int
    total_actual_debit_dollars: float
    orders: List[HedgeOrderHistoryRow]
    notes: List[str] = []
 
 
# ── Response for GET /hedge/orders/monitor ────────────────────────────────────
class OrderCheckResultSchema(BaseModel):
    client_order_id: str
    broker_order_id: Optional[str] = None
    previous_state: str
    new_state: str
    changed: bool
    is_stale: bool = False
    fill_price: Optional[float] = None
    filled_qty: Optional[float] = None
    actual_debit_dollars: Optional[float] = None
    error: Optional[str] = None
 
 
class RepriceResultSchema(BaseModel):
    original_client_order_id: str
    new_client_order_id: Optional[str] = None
    old_limit_price: float
    new_limit_price: float
    reprice_number: int
    submitted: bool
    broker_order_id: Optional[str] = None
    status: str
    message: str
 
 
class OrderMonitorResponse(BaseModel):
    as_of_date: str
    orders_checked: int
    newly_filled: List[OrderCheckResultSchema] = []
    newly_cancelled: List[OrderCheckResultSchema] = []
    stale_orders: List[OrderCheckResultSchema] = []
    reprice_results: List[RepriceResultSchema] = []
    errors: List[OrderCheckResultSchema] = []
    action_needed: bool = False
    notes: List[str] = []


class PostFillReconciliationResponse(BaseModel):
    as_of_date: str
    triggered_by: List[str]                    # client_order_ids that triggered this
 
    # Hedge state after fill
    current_hedge_pct: float
    recommended_hedge_pct: float
    remaining_gap_pct: float
    remaining_gap_dollars: float
    remaining_budget_dollars: float
 
    # Alpaca sleeve — was $0 before fills, now populated
    alpaca_hedge_exposure_dollars: float
 
    # Decision
    target_met: bool
    needs_more_hedge: bool
    immediate_actions: List[str] = []
 
    error: Optional[str] = None
    notes: List[str] = []

class CrashScenarioRow(BaseModel):
    drop_pct: float
    drop_label: str                          # "-10%", "-20%" etc.
    portfolio_loss_dollars: float
    structural_gain_dollars: float
    option_gain_dollars: float
    total_hedge_gain_dollars: float
    net_dollars: float                       # negative = net loss, positive = net gain
    hedge_offset_pct: float                  # fraction of loss covered by hedges
    structural_decay_factor: float
    option_convexity_factor: float
 
 
class CrashSimulationResponse(BaseModel):
    as_of_date: str
    market_regime: str

    # Portfolio inputs
    portfolio_value: float
    portfolio_beta: float
    portfolio_crash_beta: float
    portfolio_crash_beta_dollars: float

    # Current hedge inputs
    structural_hedge_exposure_dollars: float
    option_hedge_exposure_dollars: float
    total_hedge_exposure_dollars: float

    # Fully-hedged comparison inputs
    current_hedge_pct: float
    recommended_hedge_pct: float
    fully_hedged_structural_dollars: float
    fully_hedged_option_dollars: float

    # Scenario results — current hedge
    scenarios: List[CrashScenarioRow]

    # Scenario results — at recommended hedge level
    scenarios_fully_hedged: List[CrashScenarioRow]

    notes: List[str] = []