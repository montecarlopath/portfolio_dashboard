// src/features/hedge-dashboard/components/HedgeDashboard.tsx
"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ShieldAlert, ShieldCheck, ShieldOff } from "lucide-react";
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import type { CrashScenarioRow, HedgeOrderHistoryRow, EodAlert } from "@/lib/api";
import { useHedgeDashboardBundle } from "@/features/hedge-dashboard/hooks/useHedgeDashboardData";

function fmtDollars(n: number) {
    return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 0,
    }).format(n);
}
function fmtPct(n: number, decimals = 1) {
    return (n * 100).toFixed(decimals) + "%";
}
function fmtShort(n: number) {
    if (Math.abs(n) >= 1_000_000) return "$" + (n / 1_000_000).toFixed(1) + "M";
    if (Math.abs(n) >= 1_000) return "$" + (n / 1_000).toFixed(0) + "k";
    return fmtDollars(n);
}

const REGIME_LABELS: Record<string, string> = {
    strong_bull: "Strong Bull",
    extended_bull: "Extended Bull",
    neutral: "Neutral",
    early_breakdown: "Early Breakdown",
    high_crash_risk: "High Crash Risk",
    localized_bubble: "Localized Bubble",
};

function RegimeBadge({ regime, score }: { regime: string; score: number }) {
    const label = REGIME_LABELS[regime] ?? regime;
    const cls =
        regime === "strong_bull"
            ? "bg-emerald-500/15 text-emerald-600 border-emerald-500/30"
            : regime === "extended_bull"
                ? "bg-emerald-500/10 text-emerald-500 border-emerald-500/20"
                : regime === "neutral"
                    ? "bg-secondary text-foreground border-border"
                    : regime === "early_breakdown"
                        ? "bg-amber-500/15 text-amber-600 border-amber-500/30"
                        : regime === "high_crash_risk"
                            ? "bg-red-500/15 text-red-500 border-red-500/30"
                            : "bg-secondary text-foreground border-border";

    return (
        <span
            className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
                cls
            )}
        >
            {label}
            <span className="opacity-70">· {score.toFixed(2)}</span>
        </span>
    );
}

function HedgeGaugeBar({ current, recommended }: { current: number; recommended: number }) {
    const currentPct = Math.min(current * 100, 100);
    const recPct = Math.min(recommended * 100, 100);
    const met = current >= recommended;

    return (
        <div className="space-y-1.5">
            <div className="flex justify-between text-xs text-muted-foreground">
                <span>0%</span>
                <span>50%</span>
                <span>100%</span>
            </div>
            <div
                className="relative overflow-hidden rounded-full"
                style={{ height: "8px", background: "rgba(255,255,255,0.08)" }}
            >
                <div
                    className="absolute inset-y-0 left-0 rounded-full transition-all"
                    style={{
                        background: met ? "#10b981" : "#f59e0b",
                        width: `${currentPct}%`,
                        boxShadow: met ? "0 0 6px #10b981" : "0 0 6px #f59e0b",
                    }}
                />
            </div>
            <div className="relative h-1">
                <div
                    className="absolute rounded-full"
                    style={{
                        left: `${recPct}%`,
                        top: "-2px",
                        width: "2px",
                        height: "12px",
                        background: "rgba(255,255,255,0.5)",
                    }}
                    title={`Recommended: ${fmtPct(recommended)}`}
                />
            </div>
            <div className="flex justify-between text-xs">
                <span className={cn("font-medium", met ? "text-emerald-500" : "text-amber-500")}>
                    Current {fmtPct(current)}
                </span>
                <span className="text-muted-foreground">Target {fmtPct(recommended)}</span>
            </div>
        </div>
    );
}

function Stat({
    label,
    value,
    sub,
    valueClass,
}: {
    label: string;
    value: string;
    sub?: string;
    valueClass?: string;
}) {
    return (
        <div className="space-y-0.5">
            <p className="text-xs text-muted-foreground">{label}</p>
            <p className={cn("text-lg font-semibold leading-tight", valueClass)}>{value}</p>
            {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
        </div>
    );
}

function SectionHeader({ title, right }: { title: string; right?: React.ReactNode }) {
    return (
        <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-foreground/80">{title}</h2>
            {right}
        </div>
    );
}



function LifecycleBadge({ state }: { state: string }) {
    const variants: Record<string, string> = {
        submitted: "bg-blue-500/15 text-blue-500 border-blue-500/30",
        open: "bg-blue-500/15 text-blue-500 border-blue-500/30",
        filled: "bg-emerald-500/15 text-emerald-600 border-emerald-500/30",
        cancelled: "bg-secondary text-muted-foreground border-border",
        expired: "bg-secondary text-muted-foreground border-border",
        replaced: "bg-secondary text-muted-foreground border-border",
        failed: "bg-red-500/15 text-red-500 border-red-500/30",
    };

    return (
        <span
            className={cn(
                "inline-flex rounded-full border px-2 py-0.5 text-xs font-medium",
                variants[state] ?? "bg-secondary"
            )}
        >
            {state}
        </span>
    );
}

function CrashRow({
    row,
    fullRow,
    selected,
}: {
    row: CrashScenarioRow;
    fullRow: CrashScenarioRow;
    selected: boolean;
}) {
    const improvement = fullRow.net_dollars - row.net_dollars;

    return (
        <tr className={cn("border-b border-border/40 text-sm last:border-0", selected && "bg-accent/30")}>
            <td className="whitespace-nowrap py-2.5 pr-4 font-medium">{row.drop_label}</td>
            <td className="whitespace-nowrap py-2.5 pr-4 text-red-400">{fmtShort(row.portfolio_loss_dollars)}</td>
            <td className="whitespace-nowrap py-2.5 pr-4">
                <span>{fmtShort(Math.abs(row.net_dollars))}</span>
                <span
                    className={cn(
                        "ml-1 text-xs",
                        row.net_dollars >= 0 ? "text-emerald-500" : "text-red-400"
                    )}
                >
                    {row.net_dollars >= 0 ? "gain" : "loss"}
                </span>
            </td>
            <td className="whitespace-nowrap py-2.5 pr-4 text-muted-foreground">
                {fmtPct(row.hedge_offset_pct, 0)}
            </td>
            <td className="whitespace-nowrap py-2.5">
                {improvement > 0 ? (
                    <span className="text-xs text-emerald-500">+{fmtShort(improvement)}</span>
                ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                )}
                <span className="block text-xs text-muted-foreground">
                    {fmtPct(fullRow.hedge_offset_pct, 0)} if full
                </span>
            </td>
        </tr>
    );
}

function EodAlertsPanel({ alerts }: { alerts: EodAlert[] }) {
    if (!alerts || alerts.length === 0) return null;

    return (
        <Card className="border border-amber-500/30 bg-amber-500/5">
            <div className="px-4 pb-2 pt-4">
                <div className="flex items-center justify-between">
                    <h3 className="text-xs font-semibold uppercase tracking-widest text-amber-500">
                        ⚠ EOD Hedge Alerts
                    </h3>
                    <span className="text-xs text-muted-foreground">
                        {alerts.length} alert{alerts.length !== 1 ? "s" : ""} today
                    </span>
                </div>
            </div>
            <div className="space-y-2 px-4 pb-4">
                {alerts.map((alert, i) => (
                    <div key={i} className="rounded-md border border-amber-500/20 bg-background p-3">
                        <div className="mb-1 flex flex-wrap items-center gap-2">
                            <span
                                className="rounded px-1.5 py-0.5 text-xs font-medium"
                                style={{
                                    background:
                                        alert.alert_type === "wide_spread"
                                            ? "rgba(245,158,11,0.15)"
                                            : "rgba(239,68,68,0.15)",
                                    color: alert.alert_type === "wide_spread" ? "#f59e0b" : "#ef4444",
                                }}
                            >
                                {alert.alert_type === "wide_spread" ? "Wide spread" : "No fill"}
                            </span>
                            <span className="text-xs capitalize text-muted-foreground">{alert.bucket}</span>
                            {alert.width_pct !== null && (
                                <span className="text-xs text-muted-foreground">
                                    {(alert.width_pct * 100).toFixed(1)}% wide
                                </span>
                            )}
                        </div>
                        <p className="text-xs leading-relaxed text-muted-foreground">{alert.message}</p>
                    </div>
                ))}
                <p className="pt-1 text-xs text-muted-foreground">
                    Alerts clear automatically when orders fill. You can manually submit via the orders
                    endpoint if needed.
                </p>
            </div>
        </Card>
    );
}

export function HedgeDashboard() {
    const [cancellingId, setCancellingId] = useState<string | null>(null);
    const [monitorRunning, setMonitorRunning] = useState(false);
    const [highlightDrop, setHighlightDrop] = useState<number>(0.10);
    const qc = useQueryClient();

    const {
        data: bundle,
        isLoading: bundleLoading,
        error: bundleError,
    } = useHedgeDashboardBundle();

    const intel = bundle?.hedge_intelligence;
    const sim = bundle?.crash_sim;
    const history = bundle?.history_30d;

    // Bundle currently does not return these yet. Keep safe fallbacks.
    const eodAlerts: EodAlert[] = [];
    const orders = {
        orders: [] as HedgeOrderHistoryRow[],
        filled: 0,
        total_actual_debit_dollars: 0,
    };
    const ordersLoading = false;

    async function handleCancel(order: HedgeOrderHistoryRow) {
        if (!order.broker_order_id) return;
        setCancellingId(order.client_order_id);
        try {
            const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
            const res = await fetch(
                `${API_BASE}/hedge/orders/cancel?broker_order_id=${encodeURIComponent(order.broker_order_id)}`,
                { method: "POST", cache: "no-store" }
            );
            const data = await res.json();
            if (!data.canceled && data.message) {
                console.warn("Cancel result:", data.message);
                alert(`Cancel note: ${data.message}`);
            }
            await qc.invalidateQueries({ queryKey: ["hedge-dashboard-bundle"] });
        } catch (e) {
            console.error("Cancel failed:", e);
            alert("Cancel request failed — see console for details.");
        } finally {
            setCancellingId(null);
        }
    }

    async function handleRunMonitor() {
        setMonitorRunning(true);
        try {
            await api.runOrderMonitor();
            await qc.invalidateQueries({ queryKey: ["hedge-dashboard-bundle"] });
        } finally {
            setMonitorRunning(false);
        }
    }

    const openOrders =
        orders?.orders.filter((o: HedgeOrderHistoryRow) => ["submitted", "open"].includes(o.lifecycle_state)) ??
        [];
    const recentOrders = orders?.orders.slice(0, 10) ?? [];

    const hedgeIcon = !intel
        ? null
        : intel.current_hedge_pct >= intel.recommended_hedge_pct
            ? <ShieldCheck className="h-4 w-4 text-emerald-500" />
            : intel.additional_hedge_pct > 0.05
                ? <ShieldAlert className="h-4 w-4 text-amber-500" />
                : <ShieldOff className="h-4 w-4 text-muted-foreground" />;

    const historyData =
        history?.rows?.map((r) => ({
            date: r.date.slice(5),
            hedge_pct: parseFloat((r.current_hedge_pct * 100).toFixed(1)),
            recommended: intel ? parseFloat((intel.recommended_hedge_pct * 100).toFixed(1)) : 0,
        })) ?? [];

    if (bundleLoading) {
        return (
            <div className="flex min-h-screen items-center justify-center bg-background">
                <p className="text-sm text-muted-foreground">Loading hedge data…</p>
            </div>
        );
    }

    if (bundleError || !intel) {
        return (
            <div className="flex min-h-screen items-center justify-center bg-background">
                <p className="text-sm text-red-400">Failed to load hedge dashboard bundle.</p>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-background px-4 py-6 sm:px-6 lg:px-8">
            <div className="mx-auto max-w-7xl space-y-6">
                <div className="flex flex-wrap items-start justify-between gap-4">
                    <div>
                        <h1 className="text-2xl font-semibold tracking-tight">Hedge Dashboard</h1>
                        <p className="mt-0.5 text-sm text-muted-foreground">
                            {intel.as_of_date} · {intel.benchmark}
                        </p>
                    </div>
                    <div className="flex items-center gap-3">
                        <RegimeBadge regime={intel.market_regime} score={intel.market_risk_score} />
                        <button
                            onClick={handleRunMonitor}
                            disabled={monitorRunning}
                            className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
                        >
                            <RefreshCw className={cn("h-3.5 w-3.5", monitorRunning && "animate-spin")} />
                            {monitorRunning ? "Checking…" : "Run Monitor"}
                        </button>
                    </div>
                </div>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    <Card className="border-border/50">
                        <CardContent className="space-y-4 p-5">
                            <div className="flex items-center gap-2">
                                {hedgeIcon}
                                <p className="text-sm font-medium text-foreground/80">Hedge Status</p>
                            </div>
                            <HedgeGaugeBar
                                current={intel.current_hedge_pct}
                                recommended={intel.recommended_hedge_pct}
                            />
                            <div className="grid grid-cols-3 gap-3 pt-1">
                                <Stat
                                    label="Current"
                                    value={fmtPct(intel.current_hedge_pct)}
                                    sub={fmtShort(intel.current_hedge_exposure_dollars)}
                                    valueClass={
                                        intel.current_hedge_pct >= intel.recommended_hedge_pct
                                            ? "text-emerald-500"
                                            : "text-amber-500"
                                    }
                                />
                                <Stat
                                    label="Target"
                                    value={fmtPct(intel.recommended_hedge_pct)}
                                    sub={fmtShort(intel.recommended_hedge_exposure_dollars)}
                                />
                                <Stat
                                    label="Gap"
                                    value={fmtPct(intel.additional_hedge_pct)}
                                    sub={fmtShort(intel.additional_hedge_exposure_dollars)}
                                    valueClass={
                                        intel.additional_hedge_pct > 0.01
                                            ? "text-amber-500"
                                            : "text-emerald-500"
                                    }
                                />
                            </div>
                        </CardContent>
                    </Card>

                    <Card className="border-border/50">
                        <CardContent className="space-y-4 p-5">
                            <p className="text-sm font-medium text-foreground/80">Premium Budget</p>
                            <div className="grid grid-cols-2 gap-4">
                                <Stat label="Total budget" value={fmtShort(intel.hedge_budget_dollars)} />
                                <Stat
                                    label="Remaining"
                                    value={fmtShort(intel.remaining_hedge_budget_dollars)}
                                    valueClass="text-emerald-500"
                                />
                                <Stat label="Premium cost" value={fmtShort(intel.current_hedge_premium_cost)} />
                                <Stat
                                    label="Unrealized P&L"
                                    value={fmtShort(intel.hedge_unrealized_pnl)}
                                    valueClass={
                                        intel.hedge_unrealized_pnl >= 0
                                            ? "text-emerald-500"
                                            : "text-red-400"
                                    }
                                />
                            </div>
                        </CardContent>
                    </Card>

                    <Card className="border-border/50">
                        <CardContent className="space-y-4 p-5">
                            <p className="text-sm font-medium text-foreground/80">Hedge Sources</p>
                            <div className="space-y-3">
                                {(["composer", "alpaca"] as const).map((source) => {
                                    const s = intel.hedge_source_breakdown[source];
                                    if (!s) return null;
                                    const pct =
                                        intel.portfolio_value > 0
                                            ? s.current_hedge_exposure_dollars / intel.portfolio_value
                                            : 0;
                                    const barWidth =
                                        intel.recommended_hedge_pct > 0
                                            ? Math.min((pct / intel.recommended_hedge_pct) * 100, 100)
                                            : 0;

                                    return (
                                        <div key={source} className="space-y-1">
                                            <div className="flex items-center justify-between text-xs">
                                                <span className="font-medium capitalize">{source}</span>
                                                <span className="text-muted-foreground">
                                                    {fmtShort(s.current_hedge_exposure_dollars)}
                                                    <span className="ml-1 opacity-60">{fmtPct(pct)}</span>
                                                </span>
                                            </div>
                                            <div
                                                className="h-1.5 overflow-hidden rounded-full"
                                                style={{ background: "rgba(255,255,255,0.08)" }}
                                            >
                                                <div
                                                    className="h-full rounded-full transition-all"
                                                    style={{
                                                        background:
                                                            source === "composer"
                                                                ? "var(--chart-1)"
                                                                : "var(--chart-2)",
                                                        width: `${barWidth}%`,
                                                    }}
                                                />
                                            </div>
                                            <p className="text-xs text-muted-foreground">
                                                {s.positions_count} positions · {s.option_positions_count} options
                                                {s.current_hedge_premium_cost > 0 &&
                                                    ` · ${fmtShort(s.current_hedge_premium_cost)} premium`}
                                            </p>
                                        </div>
                                    );
                                })}
                            </div>
                        </CardContent>
                    </Card>
                </div>

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                    <Card className="border-border/50">
                        <CardContent className="p-5">
                            <p className="mb-4 text-sm font-medium text-foreground/80">Portfolio Risk</p>
                            <div className="grid grid-cols-3 gap-4">
                                <Stat label="Portfolio value" value={fmtShort(intel.portfolio_value)} />
                                <Stat label="Beta (SPY)" value={intel.portfolio_beta.toFixed(3)} />
                                <Stat
                                    label="Crash beta"
                                    value={intel.portfolio_crash_beta.toFixed(3)}
                                    valueClass="text-amber-500"
                                />
                                <Stat label="Dollar beta" value={fmtShort(intel.portfolio_dollar_beta)} />
                                <Stat
                                    label="Structural hedge"
                                    value={fmtShort(intel.structural_hedge_exposure_dollars)}
                                />
                                <Stat
                                    label="Option hedge"
                                    value={fmtShort(intel.option_hedge_exposure_dollars)}
                                />
                            </div>
                        </CardContent>
                    </Card>

                    <Card className="border-border/50">
                        <CardContent className="p-5">
                            <p className="mb-3 text-sm font-medium text-foreground/80">Regime Signals</p>
                            <div className="space-y-2">
                                {intel.reasons.map((r, i) => (
                                    <p key={i} className="flex items-start gap-2 text-sm text-muted-foreground">
                                        <span className="mt-1 h-1.5 w-1.5 flex-shrink-0 rounded-full bg-amber-500" />
                                        {r}
                                    </p>
                                ))}
                                {intel.insights.map((r, i) => (
                                    <p
                                        key={`ins-${i}`}
                                        className="flex items-start gap-2 text-sm text-muted-foreground"
                                    >
                                        <span className="mt-1 h-1.5 w-1.5 flex-shrink-0 rounded-full bg-blue-400" />
                                        {r}
                                    </p>
                                ))}
                            </div>
                        </CardContent>
                    </Card>
                </div>

                {sim && (
                    <Card className="border-border/50">
                        <CardContent className="p-5">
                            <SectionHeader
                                title="Crash Simulation"
                                right={
                                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                                        <span>Highlight drop:</span>
                                        {[0.05, 0.10, 0.20, 0.30].map((d) => (
                                            <button
                                                key={d}
                                                onClick={() => setHighlightDrop(d)}
                                                className={cn(
                                                    "rounded border px-2 py-0.5 transition-colors",
                                                    highlightDrop === d
                                                        ? "border-primary bg-primary text-primary-foreground"
                                                        : "border-border hover:bg-accent"
                                                )}
                                            >
                                                {fmtPct(d, 0)}
                                            </button>
                                        ))}
                                    </div>
                                }
                            />
                            <div className="overflow-x-auto">
                                <table className="w-full">
                                    <thead>
                                        <tr className="border-b border-border/60">
                                            {["Drop", "Portfolio loss", "Net result", "Coverage", "At full hedge"].map(
                                                (h: string) => (
                                                    <th
                                                        key={h}
                                                        className="pb-2 pr-4 text-left text-xs font-medium text-muted-foreground"
                                                    >
                                                        {h}
                                                    </th>
                                                )
                                            )}
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {sim.scenarios.map((row: CrashScenarioRow, i: number) => (
                                            <CrashRow
                                                key={row.drop_label}
                                                row={row}
                                                fullRow={sim.scenarios_fully_hedged[i]}
                                                selected={Math.abs(row.drop_pct - highlightDrop) < 0.001}
                                            />
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                            <p className="mt-3 text-xs text-muted-foreground">
                                Current hedge {fmtPct(sim.current_hedge_pct)} · Target{" "}
                                {fmtPct(sim.recommended_hedge_pct)} · At full hedge scale factor{" "}
                                {(sim.recommended_hedge_pct / Math.max(sim.current_hedge_pct, 0.01)).toFixed(2)}x
                            </p>
                        </CardContent>
                    </Card>
                )}

                {eodAlerts.length > 0 && <EodAlertsPanel alerts={eodAlerts} />}

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                    <Card className="border-border/50">
                        <CardContent className="p-5">
                            <SectionHeader
                                title={`Open Orders (${openOrders.length})`}
                                right={
                                    openOrders.length > 0 ? (
                                        <span className="text-xs font-medium text-amber-500">Pending fill</span>
                                    ) : (
                                        <span className="text-xs text-emerald-500">None open</span>
                                    )
                                }
                            />
                            {openOrders.length === 0 ? (
                                <p className="py-4 text-center text-sm text-muted-foreground">No open orders.</p>
                            ) : (
                                <div className="space-y-3">
                                    {openOrders.map((o: HedgeOrderHistoryRow) => (
                                        <div
                                            key={o.client_order_id}
                                            className="flex items-start justify-between gap-3 rounded-lg border border-border/50 p-3"
                                        >
                                            <div className="min-w-0 space-y-1">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <LifecycleBadge state={o.lifecycle_state} />
                                                    {o.ticket_bucket && (
                                                        <span className="text-xs capitalize text-muted-foreground">
                                                            {o.ticket_bucket}
                                                        </span>
                                                    )}
                                                </div>
                                                <p className="truncate font-mono text-xs text-muted-foreground">
                                                    {o.client_order_id}
                                                </p>
                                                <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
                                                    {o.estimated_debit_dollars != null && (
                                                        <span>Est. debit: {fmtShort(o.estimated_debit_dollars)}</span>
                                                    )}
                                                    {o.qty != null && <span>Qty: {o.qty}</span>}
                                                    {o.reprice_count > 0 && (
                                                        <span>Repriced ×{o.reprice_count}</span>
                                                    )}
                                                </div>
                                                {o.submitted_at_utc && (
                                                    <p className="text-xs text-muted-foreground">
                                                        {new Date(o.submitted_at_utc).toLocaleString()}
                                                    </p>
                                                )}
                                            </div>
                                            {o.broker_order_id && (
                                                <button
                                                    onClick={() => handleCancel(o)}
                                                    disabled={cancellingId === o.client_order_id}
                                                    className="flex-shrink-0 rounded border border-destructive/50 px-2.5 py-1 text-xs text-destructive transition-colors hover:bg-destructive/10 disabled:opacity-50"
                                                >
                                                    {cancellingId === o.client_order_id ? "…" : "Cancel"}
                                                </button>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    <Card className="border-border/50">
                        <CardContent className="p-5">
                            <SectionHeader
                                title="Order History"
                                right={
                                    <span className="text-xs text-muted-foreground">
                                        {orders.filled} filled · {fmtShort(orders.total_actual_debit_dollars)} actual
                                        spend
                                    </span>
                                }
                            />
                            {ordersLoading ? (
                                <p className="py-4 text-center text-sm text-muted-foreground">Loading…</p>
                            ) : recentOrders.length === 0 ? (
                                <p className="py-4 text-center text-sm text-muted-foreground">No orders yet.</p>
                            ) : (
                                <div className="space-y-2">
                                    {recentOrders.map((o: HedgeOrderHistoryRow) => (
                                        <div
                                            key={o.client_order_id}
                                            className="flex items-center justify-between gap-2 border-b border-border/40 py-1.5 text-sm last:border-0"
                                        >
                                            <div className="flex min-w-0 items-center gap-2">
                                                <LifecycleBadge state={o.lifecycle_state} />
                                                <span className="truncate text-xs capitalize text-muted-foreground">
                                                    {o.ticket_bucket ?? o.ticket_action ?? "—"}
                                                </span>
                                            </div>
                                            <div className="flex-shrink-0 space-y-0.5 text-right text-xs text-muted-foreground">
                                                {o.actual_debit_dollars != null ? (
                                                    <p className="font-medium text-foreground">
                                                        {fmtShort(o.actual_debit_dollars)} actual
                                                    </p>
                                                ) : o.estimated_debit_dollars != null ? (
                                                    <p>{fmtShort(o.estimated_debit_dollars)} est.</p>
                                                ) : null}
                                                {o.avg_fill_price != null && <p>@ ${o.avg_fill_price.toFixed(2)}</p>}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </CardContent>
                    </Card>
                </div>

                {historyData.length > 1 && (
                    <Card className="border-border/50">
                        <CardContent className="p-5">
                            <SectionHeader
                                title="Hedge Coverage — 30 Days"
                                right={
                                    <span className="text-xs text-muted-foreground">
                                        <span
                                            className="mr-1 inline-block h-0.5 w-3 align-middle"
                                            style={{ background: "#10b981" }}
                                        />
                                        current
                                        <span
                                            className="ml-3 mr-1 inline-block h-0.5 w-3 align-middle"
                                            style={{ background: "#475569" }}
                                        />
                                        target
                                    </span>
                                }
                            />
                            <div className="h-48" style={{ width: "100%", minHeight: "192px" }}>
                                <ResponsiveContainer width="100%" height={192}>
                                    <AreaChart
                                        data={historyData}
                                        margin={{ top: 4, right: 4, left: -16, bottom: 0 }}
                                    >
                                        <defs>
                                            <linearGradient id="hedgeGrad" x1="0" y1="0" x2="0" y2="1">
                                                <stop offset="5%" stopColor="#10b981" stopOpacity={0.2} />
                                                <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                                            </linearGradient>
                                        </defs>
                                        <XAxis
                                            dataKey="date"
                                            tick={{ fontSize: 11, fill: "#888" }}
                                            axisLine={false}
                                            tickLine={false}
                                            interval="preserveStartEnd"
                                        />
                                        <YAxis
                                            tick={{ fontSize: 11, fill: "#888" }}
                                            axisLine={false}
                                            tickLine={false}
                                            tickFormatter={(v) => `${v}%`}
                                            domain={[0, 20]}
                                        />
                                        <Tooltip
                                            contentStyle={{
                                                backgroundColor: "#1c1c1c",
                                                border: "1px solid #333",
                                                borderRadius: "8px",
                                                fontSize: "12px",
                                            }}
                                            formatter={(value) => [`${value}%`, "Hedge %"]}
                                        />
                                        <ReferenceLine
                                            y={intel.recommended_hedge_pct * 100}
                                            stroke="#475569"
                                            strokeDasharray="4 4"
                                            strokeWidth={1.5}
                                        />
                                        <Area
                                            type="monotone"
                                            dataKey="hedge_pct"
                                            stroke="#10b981"
                                            strokeWidth={2}
                                            fill="url(#hedgeGrad)"
                                            dot={false}
                                            name="Hedge %"
                                        />
                                    </AreaChart>
                                </ResponsiveContainer>
                            </div>
                        </CardContent>
                    </Card>
                )}
            </div>
        </div>
    );
}