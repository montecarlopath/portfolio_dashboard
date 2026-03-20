"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
    useHedgeIntelligence,
    useHedgeReconcile,
    useHedgePlan,
    useHedgeSelect,
    useHedgeRoll,
    useHedgeTickets,
    useHedgeOrderHistory,
    useEodAlerts,
    useHedgeHistory,
    useCrashSim,
} from "../hooks/useHedgeDashboardData";

const fmt$ = (n) => n == null ? "—" : `$${Math.round(Math.abs(n)).toLocaleString()}`;
const fmtPct = (n, d = 1) => n == null ? "—" : `${(n * 100).toFixed(d)}%`;
const fmtN = (n, d = 2) => n == null ? "—" : Number(n).toFixed(d);
const fmtTime = (s) => { if (!s) return "—"; try { return new Date(s).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: true }); } catch { return s; } };

const STATUS = {
    ok: { bg: "var(--color-background-success)", color: "var(--color-text-success)", label: "OK" },
    warn: { bg: "var(--color-background-warning)", color: "var(--color-text-warning)", label: "WARN" },
    err: { bg: "var(--color-background-danger)", color: "var(--color-text-danger)", label: "ERR" },
    info: { bg: "var(--color-background-info)", color: "var(--color-text-info)", label: "INFO" },
    idle: { bg: "var(--color-background-secondary)", color: "var(--color-text-secondary)", label: "—" },
};

function Badge({ type = "idle", label }) {
    const s = STATUS[type] || STATUS.idle;
    return <span style={{ background: s.bg, color: s.color, fontSize: 11, fontWeight: 500, padding: "2px 8px", borderRadius: 6, letterSpacing: "0.03em" }}>{label || s.label}</span>;
}

function Card({ title, layer, status = "idle", children }) {
    const colors = {
        L1: "#0D9488", L2: "#7C3AED", L3: "#D97706", L4: "#2563EB", L5: "#EA580C", L6: "#DC2626", L7: "#16A34A", L8: "#475569"
    };
    const c = colors[layer] || "#888";
    return (
        <div style={{ background: "var(--color-background-primary)", border: "0.5px solid var(--color-border-tertiary)", borderRadius: "var(--border-radius-lg)", borderLeft: `3px solid ${c}`, padding: "12px 14px", marginBottom: 10 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 11, fontWeight: 500, color: c, background: `${c}18`, padding: "1px 7px", borderRadius: 4 }}>{layer}</span>
                    <span style={{ fontSize: 13, fontWeight: 500, color: "var(--color-text-primary)" }}>{title}</span>
                </div>
                <Badge type={status} />
            </div>
            {children}
        </div>
    );
}

function Row({ label, value, highlight }) {
    return (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", padding: "3px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>
            <span style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>{label}</span>
            <span style={{ fontSize: 13, fontWeight: highlight ? 500 : 400, color: highlight ? "var(--color-text-primary)" : "var(--color-text-secondary)", fontFamily: "var(--font-mono)" }}>{value}</span>
        </div>
    );
}

function Grid({ children, cols = 3 }) {
    return <div style={{ display: "grid", gridTemplateColumns: `repeat(${cols},minmax(0,1fr))`, gap: 8, margin: "6px 0" }}>{children}</div>;
}

function Metric({ label, value, sub, color }) {
    return (
        <div style={{ background: "var(--color-background-secondary)", borderRadius: "var(--border-radius-md)", padding: "8px 10px" }}>
            <div style={{ fontSize: 11, color: "var(--color-text-secondary)", marginBottom: 2 }}>{label}</div>
            <div style={{ fontSize: 18, fontWeight: 500, color: color || "var(--color-text-primary)" }}>{value}</div>
            {sub && <div style={{ fontSize: 11, color: "var(--color-text-secondary)", marginTop: 1 }}>{sub}</div>}
        </div>
    );
}

function StateTag({ state }) {
    const m = { filled: "success", submitted: "info", open: "info", expired: "idle", cancelled: "idle", replaced: "idle", failed: "err" };
    return <Badge type={m[state] || "idle"} label={state} />;
}

function Section({ title }) {
    return <div style={{ fontSize: 11, fontWeight: 500, letterSpacing: "0.08em", color: "var(--color-text-secondary)", textTransform: "uppercase", margin: "16px 0 8px", borderTop: "0.5px solid var(--color-border-tertiary)", paddingTop: 12 }}>{title}</div>;
}

export default function EodReviewDashboard() {
    const qc = useQueryClient();
    const today = new Date().toISOString().split("T")[0];

    const { data: intelData, isLoading: intelLoading } = useHedgeIntelligence();
    const { data: reconcileData, isLoading: recLoading } = useHedgeReconcile();
    const { data: planData, isLoading: planLoading } = useHedgePlan();
    const { data: selectData, isLoading: selLoading } = useHedgeSelect();
    const { data: rollData, isLoading: rollLoading } = useHedgeRoll();
    const { data: ticketsData, isLoading: tktLoading } = useHedgeTickets("all", "preview");
    const { data: ordersData, isLoading: ordLoading } = useHedgeOrderHistory();
    const { data: eodAlertsData, isLoading: eodLoading } = useEodAlerts(today);
    const { data: historyData, isLoading: histLoading } = useHedgeHistory();
    const { data: crashData, isLoading: crashLoading } = useCrashSim();

    const load = () => {
        qc.invalidateQueries({ queryKey: ["hedge-intelligence"] });
        qc.invalidateQueries({ queryKey: ["hedge-reconcile"] });
        qc.invalidateQueries({ queryKey: ["hedge-plan"] });
        qc.invalidateQueries({ queryKey: ["hedge-select"] });
        qc.invalidateQueries({ queryKey: ["hedge-roll"] });
        qc.invalidateQueries({ queryKey: ["hedge-tickets"] });
        qc.invalidateQueries({ queryKey: ["hedge-order-history"] });
        qc.invalidateQueries({ queryKey: ["eod-alerts"] });
        qc.invalidateQueries({ queryKey: ["hedge-history"] });
        qc.invalidateQueries({ queryKey: ["hedge-crash-sim"] });
    };

    const loading = intelLoading || recLoading || planLoading || selLoading || rollLoading || tktLoading || ordLoading || eodLoading || histLoading || crashLoading;
    const refreshed = new Date().toLocaleTimeString();

    const intel = intelData || {};
    const recon = reconcileData || {};
    const plan = planData || {};
    const sel = selectData || {};
    const roll = rollData || {};
    const tickets = ticketsData || {};
    const orders = ordersData || {};
    const eodAlerts = eodAlertsData || {};
    const history = historyData || {};
    const crash = crashData || {};

    const regime = intel.market_regime || "—";
    const gapPct = intel.additional_hedge_pct;
    const gapMet = gapPct != null && gapPct <= 0.005;
    const primaryAction = recon.primary_action?.action || "—";
    const tailAction = recon.tail_action?.action || "—";
    const allOrders = orders.orders || [];
    const openOrders = allOrders.filter(o => ["submitted", "open"].includes(o.lifecycle_state));
    const filledOrders = allOrders.filter(o => o.lifecycle_state === "filled");
    const alerts = eodAlerts.alerts || [];
    const histRows = history.rows || [];
    const crashScenarios = crash.scenarios || [];
    const ticketList = tickets.tickets || [];
    const executableTickets = ticketList.filter(t => t.action === "buy_spread");
    const holdTickets = ticketList.filter(t => t.action === "hold_existing");

    if (loading) return (
        <div style={{ padding: "2rem", textAlign: "center", color: "var(--color-text-secondary)", fontSize: 14 }}>
            Loading all pipeline data...
        </div>
    );

    return (
        <div style={{ padding: "1rem 0", fontFamily: "var(--font-sans)" }}>

            {/* Header */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
                <div>
                    <div style={{ fontSize: 18, fontWeight: 500, color: "var(--color-text-primary)" }}>EOD pipeline review</div>
                    <div style={{ fontSize: 12, color: "var(--color-text-secondary)", marginTop: 2 }}>{intel.as_of_date || new Date().toISOString().split("T")[0]} · refreshed {refreshed}</div>
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <Badge type={alerts.length > 0 ? "warn" : "ok"} label={alerts.length > 0 ? `${alerts.length} alert` : "No alerts"} />
                    <Badge type={openOrders.length > 0 ? "warn" : gapMet ? "ok" : "warn"} label={openOrders.length > 0 ? `${openOrders.length} open` : "No open orders"} />
                    <button onClick={load} style={{ fontSize: 12, padding: "4px 12px" }}>Refresh</button>
                </div>
            </div>

            {/* ── LAYER 1: MARKET DATA ── */}
            <Section title="Layer 1 — Market & portfolio inputs" />
            <Card title="market_signal_read + market_regime_read" layer="L1" status={intel._error ? "err" : "ok"}>
                <Grid cols={4}>
                    <Metric label="SPY (live)" value={intel.vix_level != null ? `—` : intel.as_of_date ? "live" : "—"} />
                    <Metric label="Regime" value={regime} color={regime === "early_breakdown" ? "#D97706" : regime === "high_crash_risk" ? "#DC2626" : "var(--color-text-primary)"} />
                    <Metric label="Risk score" value={fmtN(intel.market_risk_score, 2)} color={intel.market_risk_score > 0.6 ? "#D97706" : "var(--color-text-primary)"} />
                    <Metric label="VIX" value={fmtN(intel.vix_level, 1)} />
                </Grid>
                {intel.reasons?.length > 0 && (
                    <div style={{ marginTop: 8 }}>
                        {intel.reasons.map((r, i) => <div key={i} style={{ fontSize: 12, color: "var(--color-text-secondary)", padding: "2px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>{r}</div>)}
                    </div>
                )}
            </Card>

            <Card title="portfolio_risk_read + holdings" layer="L1" status={intel._error ? "err" : "ok"}>
                <Grid cols={4}>
                    <Metric label="Portfolio value" value={fmt$(intel.portfolio_value)} />
                    <Metric label="Net beta" value={fmtN(intel.portfolio_beta, 3)} />
                    <Metric label="Crash beta" value={fmtN(intel.portfolio_crash_beta, 3)} color="#D97706" />
                    <Metric label="Dollar beta" value={fmt$(intel.portfolio_dollar_beta)} />
                </Grid>
                <Grid cols={2} style={{ marginTop: 8 }}>
                    <Metric label="Structural hedge" value={fmt$(intel.structural_hedge_exposure_dollars)} sub="PSQ + SQQQ + UVXY (already in beta)" />
                    <Metric label="Option hedge on" value={fmt$(intel.option_hedge_exposure_dollars)} sub={fmtPct(intel.option_hedge_exposure_pct) + " of portfolio"} />
                </Grid>
            </Card>

            {/* ── LAYER 2: INTELLIGENCE ── */}
            <Section title="Layer 2 — Hedge intelligence (crash-loss math)" />
            <Card title="hedge_intelligence_read — gap calculation" layer="L2" status={gapMet ? "ok" : "warn"}>
                <Grid cols={4}>
                    <Metric label="Current hedge" value={fmtPct(intel.current_hedge_pct)} sub={fmt$(intel.current_hedge_exposure_dollars)} color={gapMet ? "var(--color-text-success)" : "#D97706"} />
                    <Metric label="Recommended" value={fmtPct(intel.recommended_hedge_pct)} sub={fmt$(intel.recommended_hedge_exposure_dollars)} />
                    <Metric label="Gap to fill" value={fmtPct(intel.additional_hedge_pct)} sub={fmt$(intel.additional_hedge_exposure_dollars)} color={gapMet ? "var(--color-text-success)" : "#DC2626"} />
                    <Metric label="Budget left" value={fmt$(intel.remaining_hedge_budget_dollars)} sub={fmtPct(intel.remaining_hedge_budget_pct) + " of portfolio"} />
                </Grid>
                <div style={{ marginTop: 8, padding: "8px 10px", background: "var(--color-background-secondary)", borderRadius: "var(--border-radius-md)" }}>
                    <div style={{ fontSize: 11, color: "var(--color-text-secondary)", marginBottom: 4 }}>Crash-loss math (tail scenario)</div>
                    <div style={{ fontSize: 12, color: "var(--color-text-primary)", fontFamily: "var(--font-mono)" }}>
                        crash_loss = {fmt$(intel.portfolio_value)} × {fmtN(intel.portfolio_crash_beta, 3)} × {regime === "early_breakdown" ? "30%" : "—"}
                        {" → protection_needed = crash_loss − 12% tolerance"}
                    </div>
                </div>
                {intel.insights?.length > 0 && (
                    <div style={{ marginTop: 8 }}>
                        {intel.insights.map((r, i) => <div key={i} style={{ fontSize: 12, color: "var(--color-text-info)", padding: "2px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>{r}</div>)}
                    </div>
                )}
                <Grid cols={2} style={{ marginTop: 8 }}>
                    <Metric label="Premium cost basis" value={fmt$(intel.current_hedge_premium_cost_basis)} />
                    <Metric label="Unrealized P&L" value={fmt$(intel.hedge_unrealized_pnl)} color={intel.hedge_unrealized_pnl >= 0 ? "var(--color-text-success)" : "var(--color-text-danger)"} />
                </Grid>
            </Card>

            {/* ── LAYER 3: OPTION SELECTION ── */}
            <Section title="Layer 3 — Option selection" />
            <Card title="option_selector — spread chosen today" layer="L3" status={sel.primary_spread ? "ok" : "warn"}>
                {sel.primary_spread && (
                    <Grid cols={2}>
                        <div>
                            <div style={{ fontSize: 11, fontWeight: 500, color: "var(--color-text-secondary)", marginBottom: 6 }}>Primary spread</div>
                            <Row label="Expiry" value={sel.primary_spread.selected_expiry || "—"} />
                            <Row label="Long leg" value={sel.primary_spread.long_leg?.symbol || "—"} highlight />
                            <Row label="Short leg" value={sel.primary_spread.short_leg?.symbol || "—"} />
                            <Row label="Long bid/ask" value={sel.primary_spread.long_leg ? `${sel.primary_spread.long_leg.bid}/${sel.primary_spread.long_leg.ask}` : "—"} />
                            <Row label="Spread width" value={sel.primary_spread.spread_width != null ? `${sel.primary_spread.spread_width}pt` : "—"} />
                            <Row label="Score" value={fmtN(sel.primary_spread.long_leg?.score, 3)} />
                        </div>
                        <div>
                            <div style={{ fontSize: 11, fontWeight: 500, color: "var(--color-text-secondary)", marginBottom: 6 }}>Tail spread</div>
                            <Row label="Expiry" value={sel.tail_spread?.selected_expiry || "—"} />
                            <Row label="Long leg" value={sel.tail_spread?.long_leg?.symbol || "—"} highlight />
                            <Row label="Short leg" value={sel.tail_spread?.short_leg?.symbol || "—"} />
                            <Row label="Long bid/ask" value={sel.tail_spread?.long_leg ? `${sel.tail_spread.long_leg.bid}/${sel.tail_spread.long_leg.ask}` : "—"} />
                            <Row label="Spread width" value={sel.tail_spread?.spread_width != null ? `${sel.tail_spread.spread_width}pt` : "—"} />
                            <Row label="Score" value={fmtN(sel.tail_spread?.long_leg?.score, 3)} />
                        </div>
                    </Grid>
                )}
            </Card>

            {/* ── LAYER 4: EXECUTION PLANNING ── */}
            <Section title="Layer 4 — Execution planning + reconciliation" />
            <Card title="hedge_execution_planner — contract sizing" layer="L4" status={plan.primary_spread ? "ok" : "warn"}>
                <Grid cols={3}>
                    <div>
                        <div style={{ fontSize: 11, fontWeight: 500, color: "var(--color-text-secondary)", marginBottom: 4 }}>Primary plan</div>
                        <Row label="Contracts" value={plan.primary_spread?.contracts ?? 0} highlight />
                        <Row label="Target coverage" value={fmt$(plan.primary_spread?.target_hedge_dollars)} />
                        <Row label="Est. coverage" value={fmt$(plan.primary_spread?.estimated_coverage_dollars)} />
                        <Row label="Est. debit" value={fmt$(plan.primary_spread?.estimated_cost_dollars)} />
                        <Row label="Debit/contract" value={plan.primary_spread?.debit_per_contract != null ? `$${plan.primary_spread.debit_per_contract.toFixed(0)}` : "—"} />
                    </div>
                    <div>
                        <div style={{ fontSize: 11, fontWeight: 500, color: "var(--color-text-secondary)", marginBottom: 4 }}>Tail plan</div>
                        <Row label="Contracts" value={plan.tail_spread?.contracts ?? 0} highlight />
                        <Row label="Target coverage" value={fmt$(plan.tail_spread?.target_hedge_dollars)} />
                        <Row label="Est. coverage" value={fmt$(plan.tail_spread?.estimated_coverage_dollars)} />
                        <Row label="Est. debit" value={fmt$(plan.tail_spread?.estimated_cost_dollars)} />
                        <Row label="Debit/contract" value={plan.tail_spread?.debit_per_contract != null ? `$${plan.tail_spread.debit_per_contract.toFixed(0)}` : "—"} />
                    </div>
                    <div>
                        <div style={{ fontSize: 11, fontWeight: 500, color: "var(--color-text-secondary)", marginBottom: 4 }}>Total plan</div>
                        <Row label="Total debit" value={fmt$(plan.total_estimated_cost_dollars)} highlight />
                        <Row label="Total coverage" value={fmt$(plan.total_estimated_hedge_dollars)} />
                        <Row label="Coverage/cost" value={plan.total_estimated_cost_dollars > 0 ? `${(plan.total_estimated_hedge_dollars / plan.total_estimated_cost_dollars).toFixed(1)}×` : "—"} />
                    </div>
                </Grid>
            </Card>

            <Card title="hedge_reconciliation_engine — current vs target" layer="L4" status={roll.summary_action === "hold" ? "ok" : "warn"}>
                <Grid cols={2}>
                    <div>
                        <div style={{ fontSize: 11, fontWeight: 500, color: "var(--color-text-secondary)", marginBottom: 4 }}>Primary bucket</div>
                        <Row label="Action" value={primaryAction} highlight />
                        <Row label="Current exposure" value={fmt$(recon.primary_action?.current_exposure_dollars)} />
                        <Row label="Target exposure" value={fmt$(recon.primary_action?.target_exposure_dollars)} />
                        <Row label="Gap" value={fmt$(recon.primary_action?.exposure_gap_dollars)} />
                        <Row label="Positions" value={recon.primary_action?.current_positions?.join(", ") || "none"} />
                        <div style={{ fontSize: 11, color: "var(--color-text-secondary)", marginTop: 4, fontStyle: "italic" }}>{recon.primary_action?.reason || ""}</div>
                    </div>
                    <div>
                        <div style={{ fontSize: 11, fontWeight: 500, color: "var(--color-text-secondary)", marginBottom: 4 }}>Tail bucket</div>
                        <Row label="Action" value={tailAction} highlight />
                        <Row label="Current exposure" value={fmt$(recon.tail_action?.current_exposure_dollars)} />
                        <Row label="Target exposure" value={fmt$(recon.tail_action?.target_exposure_dollars)} />
                        <Row label="Gap" value={fmt$(recon.tail_action?.exposure_gap_dollars)} />
                        <Row label="Positions" value={recon.tail_action?.current_positions?.join(", ") || "none"} />
                        <div style={{ fontSize: 11, color: "var(--color-text-secondary)", marginTop: 4, fontStyle: "italic" }}>{recon.tail_action?.reason || ""}</div>
                    </div>
                </Grid>
                <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
                    {(recon.immediate_actions || []).map((a, i) => <Badge key={i} type="warn" label={a} />)}
                    {(recon.immediate_actions || []).length === 0 && <Badge type="ok" label="No immediate actions" />}
                </div>
            </Card>

            {/* ── LAYER 5: TICKETS ── */}
            <Section title="Layer 5 — Trade tickets (what would be submitted)" />
            <Card title="hedge_trade_ticket_engine — tickets generated" layer="L5" status={executableTickets.length > 0 ? "warn" : holdTickets.length > 0 ? "ok" : "idle"}>
                {ticketList.length === 0 ? <div style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>No tickets generated.</div> : (
                    <div>
                        {ticketList.map((t, i) => (
                            <div key={i} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "5px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>
                                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                                    <Badge type={t.action === "buy_spread" ? "warn" : t.action === "hold_existing" ? "ok" : "idle"} label={t.action} />
                                    <span style={{ fontSize: 11, color: "var(--color-text-secondary)", fontFamily: "var(--font-mono)" }}>{t.bucket || "—"}</span>
                                    {t.contracts > 0 && <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>×{t.contracts}</span>}
                                </div>
                                <div style={{ textAlign: "right" }}>
                                    {t.estimated_debit_dollars > 0 && <span style={{ fontSize: 12, fontFamily: "var(--font-mono)" }}>{fmt$(t.estimated_debit_dollars)}</span>}
                                    {t.estimated_coverage_added_dollars > 0 && <span style={{ fontSize: 11, color: "var(--color-text-secondary)", marginLeft: 6 }}>covers {fmt$(t.estimated_coverage_added_dollars)}</span>}
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </Card>

            {/* ── LAYER 6: ORDERS ── */}
            <Section title="Layer 6 — Broker execution (order history)" />
            <Card title="broker_submission_store — today's orders" layer="L6" status={filledOrders.length > 0 ? "ok" : openOrders.length > 0 ? "warn" : "idle"}>
                <Grid cols={4}>
                    <Metric label="Total orders" value={allOrders.length} />
                    <Metric label="Filled" value={filledOrders.length} color={filledOrders.length > 0 ? "var(--color-text-success)" : "var(--color-text-primary)"} />
                    <Metric label="Open/submitted" value={openOrders.length} color={openOrders.length > 0 ? "#D97706" : "var(--color-text-primary)"} />
                    <Metric label="Actual spend" value={fmt$(orders.total_actual_debit_dollars || 0)} />
                </Grid>
                {allOrders.length > 0 && (
                    <div style={{ marginTop: 8 }}>
                        {allOrders.slice(0, 8).map((o, i) => (
                            <div key={i} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "4px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>
                                <div style={{ display: "flex", gap: 8, alignItems: "center", minWidth: 0 }}>
                                    <StateTag state={o.lifecycle_state} />
                                    <span style={{ fontSize: 11, color: "var(--color-text-secondary)", fontFamily: "var(--font-mono)" }}>{(o.ticket_bucket || "—").padEnd(8)}</span>
                                    {o.qty && <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>qty {o.qty}</span>}
                                </div>
                                <div style={{ textAlign: "right", flexShrink: 0 }}>
                                    {o.actual_debit_dollars != null ? <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--color-text-success)" }}>{fmt$(o.actual_debit_dollars)} @ ${o.avg_fill_price?.toFixed(2) || "—"}</span>
                                        : o.estimated_debit_dollars != null ? <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--color-text-secondary)" }}>{fmt$(o.estimated_debit_dollars)} est.</span> : null}
                                    <span style={{ fontSize: 10, color: "var(--color-text-secondary)", marginLeft: 6 }}>{fmtTime(o.submitted_at_utc)}</span>
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </Card>

            {/* ── LAYER 7: EOD ENGINE ── */}
            <Section title="Layer 7 — EOD engine + monitoring" />
            <Card title="eod_hedge_engine — today's submission attempts" layer="L7" status={alerts.length > 0 ? "warn" : "ok"}>
                {alerts.length === 0 ? (
                    <div style={{ fontSize: 12, color: "var(--color-text-success)" }}>No EOD alerts today — all spreads within bid-ask thresholds.</div>
                ) : (
                    alerts.map((a, i) => (
                        <div key={i} style={{ padding: "6px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>
                            <div style={{ display: "flex", gap: 8, marginBottom: 2 }}>
                                <Badge type="warn" label={a.alert_type === "wide_spread" ? "Wide spread" : "No fill"} />
                                <span style={{ fontSize: 11, color: "var(--color-text-secondary)", fontFamily: "var(--font-mono)" }}>{a.bucket}</span>
                                {a.width_pct != null && <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>{(a.width_pct * 100).toFixed(1)}% wide</span>}
                            </div>
                            <div style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>{a.message}</div>
                        </div>
                    ))
                )}
                <div style={{ marginTop: 8, padding: "6px 10px", background: "var(--color-background-secondary)", borderRadius: "var(--border-radius-md)" }}>
                    <div style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>Scheduled today: 3:00 monitor → 3:15/25/35 mid price → 3:45 ask+slippage → 4:30 snapshot</div>
                </div>
            </Card>

            {/* ── LAYER 8: HISTORY ── */}
            <Section title="Layer 8 — History snapshot" />
            <Card title="hedge_snapshot_writer — DB history" layer="L8" status={histRows.length > 0 ? "ok" : "warn"}>
                <Grid cols={3}>
                    <Metric label="Rows in DB" value={histRows.length} sub="hedge_snapshots table" />
                    <Metric label="Latest date" value={histRows.at(-1)?.date || "—"} />
                    <Metric label="Latest hedge %" value={histRows.length > 0 ? fmtPct(histRows.at(-1)?.current_hedge_pct) : "—"} />
                </Grid>
                {histRows.length > 2 && (
                    <div style={{ marginTop: 8 }}>
                        <div style={{ fontSize: 11, color: "var(--color-text-secondary)", marginBottom: 4 }}>Last 5 snapshots</div>
                        {histRows.slice(-5).reverse().map((r, i) => (
                            <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "3px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>
                                <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--color-text-secondary)" }}>{r.date}</span>
                                <span style={{ fontSize: 12, fontFamily: "var(--font-mono)" }}>{fmtPct(r.current_hedge_pct)} hedge</span>
                                <span style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>{fmt$(r.portfolio_value)}</span>
                                <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>{r.market_regime || "—"}</span>
                            </div>
                        ))}
                    </div>
                )}
            </Card>

            {/* ── CRASH SIM ── */}
            <Section title="Crash simulation — current coverage" />
            <Card title="hedge/crash-sim — protection at key drop levels" layer="L2" status="info">
                {crashScenarios.length > 0 ? (
                    <div>
                        {crashScenarios.map((s, i) => {
                            const pct = Math.round(s.hedge_offset_pct * 100);
                            const net = s.net_dollars;
                            return (
                                <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, padding: "5px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>
                                    <span style={{ fontSize: 12, fontWeight: 500, minWidth: 36, color: "#DC2626" }}>{s.drop_label}</span>
                                    <span style={{ fontSize: 12, color: "var(--color-text-secondary)", minWidth: 90 }}>loss: {fmt$(s.portfolio_loss_dollars)}</span>
                                    <div style={{ flex: 1, height: 6, background: "var(--color-background-secondary)", borderRadius: 3 }}>
                                        <div style={{ width: `${Math.min(pct, 100)}%`, height: "100%", background: pct > 60 ? "#16A34A" : pct > 30 ? "#D97706" : "#DC2626", borderRadius: 3 }} />
                                    </div>
                                    <span style={{ fontSize: 12, fontWeight: 500, minWidth: 36, textAlign: "right", color: pct > 60 ? "var(--color-text-success)" : pct > 30 ? "#D97706" : "var(--color-text-danger)" }}>{pct}%</span>
                                    <span style={{ fontSize: 12, color: net >= 0 ? "var(--color-text-success)" : "var(--color-text-danger)", minWidth: 80, textAlign: "right" }}>{fmt$(net)} {net >= 0 ? "gain" : "loss"}</span>
                                </div>
                            );
                        })}
                    </div>
                ) : <div style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>No crash sim data.</div>}
            </Card>

            {/* ── ROLL ENGINE ── */}
            <Section title="Roll engine — existing position health" />
            <Card title="hedge_roll_engine — hold / roll / replace recommendations" layer="L4" status={roll.primary_decision ? "ok" : "warn"}>
                <Grid cols={2}>
                    <div>
                        <div style={{ fontSize: 11, fontWeight: 500, color: "var(--color-text-secondary)", marginBottom: 4 }}>Primary recommendation</div>
                        <Row label="Action" value={roll.primary_decision?.action || "—"} highlight />
                        <Row label="Reason" value={roll.primary_decision?.reason || "—"} />
                        <Row label="Structure" value={roll.primary_decision?.structure_name || "—"} />
                    </div>
                    <div>
                        <div style={{ fontSize: 11, fontWeight: 500, color: "var(--color-text-secondary)", marginBottom: 4 }}>Tail recommendation</div>
                        <Row label="Action" value={roll.tail_decision?.action || "—"} highlight />
                        <Row label="Reason" value={roll.tail_decision?.reason || "—"} />
                        <Row label="Structure" value={roll.tail_decision?.structure_name || "—"} />
                    </div>
                </Grid>
            </Card>

            {/* ── PIPELINE HEALTH SUMMARY ── */}
            <Section title="Pipeline health — one-line summary per layer" />
            <div style={{ background: "var(--color-background-secondary)", borderRadius: "var(--border-radius-lg)", padding: "10px 14px" }}>
                {[
                    { layer: "L1", label: "Market data", check: !intel._error, msg: intel._error || `SPY signals loaded · regime: ${regime} · beta: ${fmtN(intel.portfolio_beta, 3)}` },
                    { layer: "L2", label: "Intelligence", check: !intel._error, msg: intel._error || `Gap: ${fmtPct(intel.additional_hedge_pct)} (${fmt$(intel.additional_hedge_exposure_dollars)}) · budget: ${fmt$(intel.remaining_hedge_budget_dollars)}` },
                    { layer: "L3", label: "Option selector", check: !!sel.primary_spread, msg: sel.primary_spread ? `Primary: ${sel.primary_spread.long_leg?.symbol || "—"} · Tail: ${sel.tail_spread?.long_leg?.symbol || "—"}` : "No spread selected" },
                    { layer: "L4", label: "Execution plan", check: !!plan.primary_spread, msg: plan.primary_spread ? `${plan.primary_spread.contracts || 0} primary + ${plan.tail_spread?.contracts || 0} tail contracts · ${fmt$(plan.total_estimated_cost_dollars)} total debit` : "No plan" },
                    { layer: "L4", label: "Reconciliation", check: true, msg: `Primary: ${primaryAction} · Tail: ${tailAction} · ${(recon.immediate_actions || []).length} immediate actions` },
                    { layer: "L5", label: "Tickets", check: true, msg: `${ticketList.length} tickets: ${executableTickets.length} buy_spread, ${holdTickets.length} hold_existing` },
                    { layer: "L6", label: "Orders", check: true, msg: `${allOrders.length} orders total · ${filledOrders.length} filled · ${openOrders.length} open · ${fmt$(orders.total_actual_debit_dollars || 0)} spent` },
                    { layer: "L7", label: "EOD engine", check: alerts.length === 0, msg: alerts.length === 0 ? "All attempts OK, no alerts" : `${alerts.length} alert(s): ${alerts.map(a => a.alert_type).join(", ")}` },
                    { layer: "L8", label: "History", check: histRows.length > 0, msg: histRows.length > 0 ? `${histRows.length} snapshots · latest: ${histRows.at(-1)?.date || "—"} at ${fmtPct(histRows.at(-1)?.current_hedge_pct)}` : "No snapshots in DB" },
                ].map((r, i) => (
                    <div key={i} style={{ display: "flex", alignItems: "baseline", gap: 10, padding: "4px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>
                        <span style={{ fontSize: 10, fontWeight: 500, color: "#888", minWidth: 20 }}>{r.layer}</span>
                        <span style={{ fontSize: 12, fontWeight: 500, color: "var(--color-text-primary)", minWidth: 110 }}>{r.label}</span>
                        <span style={{ display: "inline-block", width: 6, height: 6, borderRadius: "50%", background: r.check ? "#16A34A" : "#DC2626", flexShrink: 0, marginBottom: 1 }} />
                        <span style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>{r.msg}</span>
                    </div>
                ))}
            </div>



        </div>
    );
}