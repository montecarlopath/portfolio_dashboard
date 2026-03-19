"use client";
import { useState, useEffect, useCallback } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
const fmt$ = (n) => n == null ? "—" : `$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const fmtP = (n) => n == null ? "—" : `$${n.toFixed(2)}`;
const fmtPct = (n) => n == null ? "—" : `${(n * 100).toFixed(1)}%`;

function get(path) {
    return fetch(`${API}${path}`, { cache: "no-store" }).then(r => r.json()).catch(e => ({ _error: e.message }));
}
function post(path) {
    return fetch(`${API}${path}`, { method: "POST", cache: "no-store" }).then(r => r.json()).catch(e => ({ _error: e.message }));
}

function buildSpreads(positions) {
    const opts = positions.filter(p => p.symbol?.match(/^[A-Z]+\d{6}[PC]\d{8}$/));
    const byExpiry = {};
    for (const p of opts) {
        const m = p.symbol.match(/^([A-Z]+)(\d{6})([CP])(\d{8})$/);
        if (!m) continue;
        const expiry = `20${m[2].slice(0, 2)}-${m[2].slice(2, 4)}-${m[2].slice(4, 6)}`;
        if (!byExpiry[expiry]) byExpiry[expiry] = [];
        byExpiry[expiry].push(p);
    }
    const spreads = [];
    for (const [expiry, legs] of Object.entries(byExpiry)) {
        const long = legs.find(p => p.side === "long" || p.qty > 0);
        const short = legs.find(p => p.side === "short" || p.qty < 0);
        if (!long) continue;
        const qty = Math.abs(long.qty || 0);
        const longCost = Math.abs(long.cost_basis || 0) / (qty * 100);
        const shortCredit = short ? Math.abs(short.cost_basis || 0) / (qty * 100) : 0;
        const netCost = longCost - shortCredit;
        const longMv = (long.current_price || 0);
        const shortMv = short ? (short.current_price || 0) : 0;
        const netMv = longMv - shortMv;
        const pnlPerSpread = netMv - netCost;
        spreads.push({
            expiry, qty,
            longSymbol: long.symbol,
            shortSymbol: short?.symbol || null,
            longStrike: parseInt(long.symbol.match(/\d{8}$/)?.[0] || "0") / 1000,
            shortStrike: short ? parseInt(short.symbol.match(/\d{8}$/)?.[0] || "0") / 1000 : null,
            longCurrentPrice: long.current_price,
            shortCurrentPrice: short?.current_price,
            netCostPerSpread: netCost,
            netMvPerSpread: netMv,
            pnlPerSpread,
            totalCost: netCost * qty * 100,
            totalMv: netMv * qty * 100,
            totalPnl: pnlPerSpread * qty * 100,
            multiple: netCost > 0 ? netMv / netCost : null,
            isSpread: !!short,
            rawLong: long,
            rawShort: short,
        });
    }
    return spreads.sort((a, b) => a.expiry.localeCompare(b.expiry));
}

const css = `
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=DM+Sans:wght@400;500;600&display=swap');
  .hpm { font-family: 'DM Sans', sans-serif; max-width: 900px; padding: 2rem 0; }
  .hpm-mono { font-family: 'IBM Plex Mono', monospace; }
  .hpm-hdr { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:28px; }
  .hpm-title { font-size:22px; font-weight:600; letter-spacing:-0.5px; color:var(--color-text-primary); margin:0 0 4px; }
  .hpm-sub { font-size:13px; color:var(--color-text-secondary); margin:0; }
  .hpm-btn-refresh { font-family:'IBM Plex Mono',monospace; font-size:11px; padding:6px 14px; letter-spacing:.05em;
    border:1px solid var(--color-border-secondary); border-radius:6px; cursor:pointer;
    color:var(--color-text-secondary); background:transparent; transition:all .15s; }
  .hpm-btn-refresh:hover { color:var(--color-text-primary); border-color:var(--color-border-primary); }

  .hpm-alert { border-radius:10px; padding:16px 20px; margin-bottom:28px;
    border-left:3px solid #f59e0b; background:rgba(245,158,11,.07); }
  .hpm-alert-title { font-size:12px; font-weight:600; color:#b45309; margin:0 0 6px;
    letter-spacing:.06em; text-transform:uppercase; }
  .hpm-alert-body { font-size:13px; color:var(--color-text-primary); line-height:1.6; margin:0 0 3px; }

  .hpm-card { border-radius:12px; margin-bottom:16px; overflow:hidden;
    border:1px solid var(--color-border-secondary); background:var(--color-background-primary); }
  .hpm-card-top { padding:20px 24px 0; display:flex; justify-content:space-between; align-items:flex-start; }
  .hpm-card-name { font-size:15px; font-weight:600; color:var(--color-text-primary); margin:0 0 5px; }
  .hpm-card-syms { font-family:'IBM Plex Mono',monospace; font-size:11px; color:var(--color-text-tertiary); letter-spacing:.02em; }
  .hpm-pnl { text-align:right; }
  .hpm-pnl-num { font-size:22px; font-weight:600; font-family:'IBM Plex Mono',monospace; }
  .hpm-pnl-lbl { font-size:11px; color:var(--color-text-tertiary); margin-top:2px; }

  .hpm-metrics { display:grid; grid-template-columns:repeat(4,1fr); gap:1px;
    margin-top:20px; border-top:1px solid var(--color-border-tertiary); background:var(--color-border-tertiary); }
  .hpm-metric { background:var(--color-background-secondary); padding:14px 20px; }
  .hpm-metric-lbl { font-size:11px; color:var(--color-text-tertiary); text-transform:uppercase; letter-spacing:.06em; margin-bottom:6px; }
  .hpm-metric-val { font-size:18px; font-weight:600; font-family:'IBM Plex Mono',monospace; color:var(--color-text-primary); }

  .hpm-legs { display:grid; grid-template-columns:1fr 1fr; gap:1px;
    border-top:1px solid var(--color-border-tertiary); background:var(--color-border-tertiary); }
  .hpm-leg { background:var(--color-background-primary); padding:12px 20px; }
  .hpm-leg-type { font-size:10px; text-transform:uppercase; letter-spacing:.08em; font-weight:700; margin-bottom:5px; }
  .hpm-leg-long .hpm-leg-type { color:#16a34a; }
  .hpm-leg-short .hpm-leg-type { color:#dc2626; }
  .hpm-leg-sym { font-family:'IBM Plex Mono',monospace; font-size:12px; color:var(--color-text-primary); margin-bottom:3px; }
  .hpm-leg-info { font-size:12px; color:var(--color-text-secondary); }

  .hpm-actions { padding:14px 24px; border-top:1px solid var(--color-border-tertiary);
    display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .hpm-qty-wrap { display:flex; align-items:center; gap:8px; }
  .hpm-qty-lbl { font-size:13px; font-weight:500; color:var(--color-text-primary); }
  .hpm-qty-in { width:68px; height:36px; text-align:center; font-family:'IBM Plex Mono',monospace;
    font-size:15px; font-weight:600; border:1.5px solid var(--color-border-secondary);
    border-radius:8px; background:var(--color-background-secondary); color:var(--color-text-primary); padding:0 8px; }
  .hpm-qty-in:focus { outline:none; border-color:#3b82f6; box-shadow:0 0 0 3px rgba(59,130,246,.15); }
  .hpm-qty-rem { font-size:12px; color:var(--color-text-tertiary); }
  .hpm-sep { width:1px; height:24px; background:var(--color-border-secondary); flex-shrink:0; }
  .hpm-postclose { font-size:12px; color:var(--color-text-secondary); }
  .hpm-postclose strong { font-family:'IBM Plex Mono',monospace; font-size:13px; }
  .c-ok { color:#16a34a; }
  .c-warn { color:#d97706; }
  .c-bad { color:#dc2626; }

  .hpm-btn { height:36px; padding:0 18px; border-radius:8px; font-size:13px; font-weight:500;
    cursor:pointer; border:1px solid; transition:all .15s; font-family:'DM Sans',sans-serif; white-space:nowrap; }
  .hpm-btn:disabled { opacity:.45; cursor:not-allowed; }
  .hpm-btn-ghost { border-color:var(--color-border-secondary); color:var(--color-text-secondary); background:transparent; }
  .hpm-btn-ghost:hover:not(:disabled) { color:var(--color-text-primary); border-color:var(--color-border-primary); background:var(--color-background-secondary); }
  .hpm-btn-danger { border-color:rgba(220,38,38,.4); color:#dc2626; background:rgba(220,38,38,.07); }
  .hpm-btn-danger:hover:not(:disabled) { background:rgba(220,38,38,.14); border-color:#dc2626; }
  .hpm-btn-confirm { border-color:#dc2626; color:#fff; background:#dc2626; }
  .hpm-btn-confirm:hover:not(:disabled) { background:#b91c1c; }

  .hpm-confirm { padding:14px 24px; border-top:1px solid rgba(245,158,11,.25);
    background:rgba(245,158,11,.05); display:flex; justify-content:space-between; align-items:center; gap:16px; }
  .hpm-confirm-title { font-size:13px; font-weight:600; color:#92400e; margin:0 0 3px; }
  .hpm-confirm-note { font-size:12px; color:#b45309; }
  .hpm-confirm-btns { display:flex; gap:8px; flex-shrink:0; }

  .hpm-result { padding:11px 24px; border-top:1px solid var(--color-border-tertiary); background:var(--color-background-secondary); }
  .hpm-result-ok { font-family:'IBM Plex Mono',monospace; font-size:12px; color:#16a34a; }
  .hpm-result-prev { font-family:'IBM Plex Mono',monospace; font-size:12px; color:var(--color-text-secondary); }
  .hpm-result-err { font-family:'IBM Plex Mono',monospace; font-size:12px; color:#dc2626; }

  .hpm-composer { margin-top:8px; border:1px solid var(--color-border-secondary);
    border-radius:10px; overflow:hidden; }
  .hpm-composer-hdr { padding:12px 20px; background:var(--color-background-secondary);
    font-size:11px; font-weight:600; color:var(--color-text-secondary);
    text-transform:uppercase; letter-spacing:.06em; border-bottom:1px solid var(--color-border-tertiary); }
  .hpm-composer-row { padding:11px 20px; display:flex; justify-content:space-between; align-items:center;
    border-bottom:1px solid var(--color-border-tertiary); }
  .hpm-composer-row:last-child { border-bottom:none; }
  .hpm-composer-sym { font-family:'IBM Plex Mono',monospace; font-size:12px; color:var(--color-text-primary); }
  .hpm-composer-note { font-size:12px; color:var(--color-text-tertiary); }
`;

export default function HedgePositionManager() {
    const [positions, setPositions] = useState([]);
    const [loading, setLoading] = useState(true);
    const [closing, setClosing] = useState({});
    const [results, setResults] = useState({});
    const [confirm, setConfirm] = useState(null);
    const [intel, setIntel] = useState(null);
    const [closeQty, setCloseQty] = useState({});

    const load = useCallback(async () => {
        setLoading(true);
        const [posResp, intelResp] = await Promise.all([
            get("/hedge/positions/alpaca"),
            get("/risk/hedge-intelligence?account_id=all"),
        ]);
        setPositions(posResp.positions || []);
        setIntel(intelResp);
        setLoading(false);
    }, []);

    useEffect(() => { load(); }, [load]);

    const spreads = buildSpreads(positions);

    async function handleClose(spread, mode = "preview") {
        const qty = closeQty[spread.expiry] ?? spread.qty;
        if (mode === "submit" && confirm !== spread.expiry) { setConfirm(spread.expiry); return; }
        setClosing(c => ({ ...c, [spread.expiry]: true }));
        setConfirm(null);
        try {
            const params = new URLSearchParams({
                account_id: "all", mode, long_symbol: spread.longSymbol, qty: String(qty),
                ...(spread.shortSymbol ? { short_symbol: spread.shortSymbol } : {}),
            });
            const result = await post(`/hedge/close-position?${params}`);
            setResults(r => ({ ...r, [spread.expiry]: result }));
            if (mode === "submit") await load();
        } catch (e) {
            setResults(r => ({ ...r, [spread.expiry]: { _error: e.message } }));
        } finally {
            setClosing(c => ({ ...c, [spread.expiry]: false }));
        }
    }

    if (loading) return (
        <>
            <style>{css}</style>
            <div className="hpm" style={{ padding: "3rem", textAlign: "center", color: "var(--color-text-secondary)", fontSize: 13 }}>Loading positions…</div>
        </>
    );

    const overHedged = intel?.current_hedge_pct > (intel?.recommended_hedge_pct || 0) * 1.5;
    const excess = intel ? intel.option_hedge_exposure_dollars - intel.recommended_hedge_exposure_dollars : 0;

    return (
        <>
            <style>{css}</style>
            <div className="hpm">

                <div className="hpm-hdr">
                    <div>
                        <h1 className="hpm-title">Hedge position manager</h1>
                        <p className="hpm-sub">Alpaca option spreads — close positions manually</p>
                    </div>
                    <button className="hpm-btn-refresh" onClick={load}>↺ Refresh</button>
                </div>

                {overHedged && (
                    <div className="hpm-alert">
                        <p className="hpm-alert-title">⚠ Portfolio is over-hedged</p>
                        <p className="hpm-alert-body">
                            Current option hedge <strong>{fmtPct(intel.current_hedge_pct)}</strong> · Recommended <strong>{fmtPct(intel.recommended_hedge_pct)}</strong> · Excess {fmt$(excess)} coverage
                        </p>
                        <p className="hpm-alert-body">Close the May-29 spread to return to target. The Jun-30 spread can stay — it's your tail protection.</p>
                    </div>
                )}

                {spreads.map(spread => {
                    const isBusy = closing[spread.expiry];
                    const result = results[spread.expiry];
                    const isConfirming = confirm === spread.expiry;
                    const qtyToClose = closeQty[spread.expiry] ?? spread.qty;
                    const remaining = spread.qty - qtyToClose;

                    const alpacaExposure = intel?.hedge_source_breakdown?.alpaca?.option_hedge_exposure_dollars || 0;
                    const totalQty = spreads.reduce((s, sp) => s + sp.qty, 0);
                    const removed = alpacaExposure * (spread.qty / Math.max(totalQty, 1)) * (qtyToClose / spread.qty);
                    const postExposure = Math.max(0, (intel?.option_hedge_exposure_dollars || 0) - removed);
                    const postPct = (intel?.portfolio_value || 1) > 0 ? postExposure / intel.portfolio_value : 0;
                    const rec = intel?.recommended_hedge_pct || 0;
                    const postClass = postPct < rec * 0.7 ? "c-bad" : postPct < rec ? "c-warn" : "c-ok";
                    const pnlPos = spread.totalPnl >= 0;

                    return (
                        <div key={spread.expiry} className="hpm-card">
                            <div className="hpm-card-top">
                                <div>
                                    <div className="hpm-card-name">{spread.qty}× put spread — expires {spread.expiry}</div>
                                    <div className="hpm-card-syms">
                                        Long {spread.longSymbol}{spread.shortSymbol ? ` / Short ${spread.shortSymbol}` : ""}
                                    </div>
                                </div>
                                <div className="hpm-pnl">
                                    <div className="hpm-pnl-num" style={{ color: pnlPos ? "#16a34a" : "#dc2626" }}>
                                        {pnlPos ? "+" : ""}{fmt$(spread.totalPnl)}
                                    </div>
                                    <div className="hpm-pnl-lbl">unrealized P&L</div>
                                </div>
                            </div>

                            <div className="hpm-metrics">
                                {[
                                    ["Net cost / spread", fmtP(spread.netCostPerSpread), null],
                                    ["Current value / spread", fmtP(spread.netMvPerSpread), null],
                                    ["P&L per spread", (spread.pnlPerSpread >= 0 ? "+" : "") + fmtP(spread.pnlPerSpread), pnlPos ? "#16a34a" : "#dc2626"],
                                    ["Value multiple", spread.multiple != null ? `${spread.multiple.toFixed(2)}×` : "—", pnlPos ? "#16a34a" : "#dc2626"],
                                ].map(([lbl, val, color], i) => (
                                    <div key={i} className="hpm-metric">
                                        <div className="hpm-metric-lbl">{lbl}</div>
                                        <div className="hpm-metric-val" style={color ? { color } : {}}>{val}</div>
                                    </div>
                                ))}
                            </div>

                            {spread.isSpread && (
                                <div className="hpm-legs">
                                    <div className="hpm-leg hpm-leg-long">
                                        <div className="hpm-leg-type">Long put</div>
                                        <div className="hpm-leg-sym">{spread.longSymbol}</div>
                                        <div className="hpm-leg-info">Strike {spread.longStrike} · price <strong>{fmtP(spread.longCurrentPrice)}</strong></div>
                                    </div>
                                    <div className="hpm-leg hpm-leg-short">
                                        <div className="hpm-leg-type">Short put</div>
                                        <div className="hpm-leg-sym">{spread.shortSymbol}</div>
                                        <div className="hpm-leg-info">Strike {spread.shortStrike} · price <strong style={{ color: "#dc2626" }}>{fmtP(spread.shortCurrentPrice)}</strong></div>
                                    </div>
                                </div>
                            )}

                            <div className="hpm-actions">
                                <div className="hpm-qty-wrap">
                                    <span className="hpm-qty-lbl">Close</span>
                                    <input type="number" min={1} max={spread.qty} value={qtyToClose}
                                        className="hpm-qty-in"
                                        onChange={e => setCloseQty(q => ({
                                            ...q, [spread.expiry]: Math.min(spread.qty, Math.max(1, parseInt(e.target.value) || 1))
                                        }))}
                                    />
                                    <span className="hpm-qty-rem">of {spread.qty}{remaining > 0 ? ` · ${remaining} remain` : " · full position"}</span>
                                </div>
                                <div className="hpm-sep" />
                                <div className="hpm-postclose">
                                    Post-close hedge: <strong className={postClass}>{fmtPct(postPct)}</strong>
                                    <span style={{ marginLeft: 4, color: "var(--color-text-tertiary)", fontSize: 11 }}>(target {fmtPct(rec)})</span>
                                </div>
                                <div className="hpm-sep" />
                                <button className="hpm-btn hpm-btn-ghost" onClick={() => handleClose(spread, "preview")} disabled={isBusy}>Preview</button>
                                <button className="hpm-btn hpm-btn-danger" onClick={() => handleClose(spread, "submit")} disabled={isBusy}>
                                    {isBusy ? "Submitting…" : `Close ${qtyToClose} spread${qtyToClose !== 1 ? "s" : ""}`}
                                </button>
                            </div>

                            {isConfirming && (
                                <div className="hpm-confirm">
                                    <div>
                                        <p className="hpm-confirm-title">Confirm: close {qtyToClose} of {spread.qty} spreads at mid price</p>
                                        <p className="hpm-confirm-note">2-leg limit order · both legs simultaneously · day order, expires 4 PM if unfilled</p>
                                    </div>
                                    <div className="hpm-confirm-btns">
                                        <button className="hpm-btn hpm-btn-ghost" onClick={() => setConfirm(null)}>Cancel</button>
                                        <button className="hpm-btn hpm-btn-confirm" onClick={() => handleClose(spread, "submit")} disabled={isBusy}>
                                            {isBusy ? "Submitting…" : "Confirm close"}
                                        </button>
                                    </div>
                                </div>
                            )}

                            {result && (
                                <div className="hpm-result">
                                    {result._error ? <div className="hpm-result-err">Error: {result._error}</div>
                                        : Array.isArray(result) ? result.map((r, i) => (
                                            <div key={i} className={r.submitted ? "hpm-result-ok" : "hpm-result-prev"}>
                                                {r.submitted
                                                    ? `✓ Submitted ${r.long_symbol || r.symbol} ×${r.contracts} @ $${r.limit_price} · broker ${(r.broker_order_id || "").slice(0, 8)}…`
                                                    : `Preview · ${r.long_symbol || r.symbol} ×${r.contracts} · limit $${r.limit_price} · ${r.message || "not submitted"}`}
                                            </div>
                                        )) : <div className="hpm-result-prev">{JSON.stringify(result)}</div>}
                                </div>
                            )}
                        </div>
                    );
                })}

                <div className="hpm-composer">
                    <div className="hpm-composer-hdr">Composer positions — manual close required</div>
                    {[
                        { sym: "QQQ260515P00550000", qty: 3, exp: "May 15, 2026" },
                        { sym: "QQQ260618P00550000", qty: 4, exp: "Jun 18, 2026" },
                    ].map(p => (
                        <div key={p.sym} className="hpm-composer-row">
                            <span className="hpm-composer-sym">{p.sym}</span>
                            <span className="hpm-composer-note">{p.qty} contracts · {p.exp} · close in Composer</span>
                        </div>
                    ))}
                </div>

            </div>
        </>
    );
}
