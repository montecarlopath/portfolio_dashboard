// src/features/hedge-dashboard/hooks/useHedgeDashboardData.ts
"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/lib/api";
import type {
    HedgeIntelligence,
    CrashSimResult,
    HedgeOrderHistoryResponse,
    HedgeHistory,
    EodAlertsResponse,
    HedgeDashboardBundle,
} from "@/lib/api";

const STALE_TIME = 60_000; // 1 min — hedge data doesn't need sub-second freshness

export function useHedgeIntelligence(accountId = "all") {
    return useQuery<HedgeIntelligence>({
        queryKey: ["hedge-intelligence", accountId],
        queryFn: () => api.getHedgeIntelligence(accountId),
        staleTime: 0,
        refetchOnMount: "always",
        refetchOnReconnect: true,
        refetchOnWindowFocus: false,
        refetchInterval: 5 * 60_000,
    });
}

export function useCrashSim(accountId = "all") {
    return useQuery<CrashSimResult>({
        queryKey: ["hedge-crash-sim", accountId],
        queryFn: () => api.getCrashSim(accountId),
        staleTime: 0,
        refetchOnMount: "always",
        refetchOnReconnect: true,
        refetchOnWindowFocus: false,
        refetchInterval: 5 * 60_000,
    });
}

export function useHedgeOrderHistory(state?: string) {
    return useQuery<HedgeOrderHistoryResponse>({
        queryKey: ["hedge-order-history", state ?? "all"],
        queryFn: () => api.getHedgeOrderHistory(20),
        staleTime: 30_000,
        refetchInterval: 60_000,
    });
}

export function useHedgeHistory(accountId = "all") {
    // Last 30 days
    const { start, end } = useMemo(() => {
        const e = new Date().toISOString().slice(0, 10);
        const s = new Date(new Date().getTime() - 30 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
        return { start: s, end: e };
    }, []);

    return useQuery<HedgeHistory>({
        queryKey: ["hedge-history", accountId, start, end],
        queryFn: () => api.getHedgeHistory(accountId, start, end),
        staleTime: STALE_TIME,
    });
}

export function useHedgeReconcile(accountId = "all") {
    return useQuery<Record<string, unknown>>({
        queryKey: ["hedge-reconcile", accountId],
        queryFn: () => api.getHedgeReconcile(accountId),
        staleTime: STALE_TIME,
        refetchInterval: 60_000,
    });
}

export function useHedgePlan(accountId = "all") {
    return useQuery<Record<string, unknown>>({
        queryKey: ["hedge-plan", accountId],
        queryFn: () => api.getHedgePlan(accountId),
        staleTime: STALE_TIME,
        refetchInterval: 60_000,
    });
}

export function useHedgeSelect(accountId = "all") {
    return useQuery<Record<string, unknown>>({
        queryKey: ["hedge-select", accountId],
        queryFn: () => api.getHedgeSelect(accountId),
        staleTime: STALE_TIME,
        refetchInterval: 60_000,
    });
}

export function useHedgeRoll(accountId = "all") {
    return useQuery<Record<string, unknown>>({
        queryKey: ["hedge-roll", accountId],
        queryFn: () => api.getHedgeRoll(accountId),
        staleTime: STALE_TIME,
        refetchInterval: 60_000,
    });
}

export function useHedgeTickets(accountId = "all", mode = "preview") {
    return useQuery<Record<string, unknown>>({
        queryKey: ["hedge-tickets", accountId, mode],
        queryFn: () => api.getHedgeTickets(accountId, mode),
        staleTime: STALE_TIME,
        refetchInterval: 60_000,
    });
}

export function useEodAlerts(date?: string) {
    const d = date || new Date().toISOString().slice(0, 10);
    return useQuery<EodAlertsResponse>({
        queryKey: ["eod-alerts", d],
        queryFn: () => api.getEodAlerts(d),
        staleTime: 60_000,
        refetchInterval: 60_000,
    });
}

export function useHedgeDashboardBundle(accountId = "all") {
    return useQuery<HedgeDashboardBundle>({
        queryKey: ["hedge-dashboard-bundle", accountId],
        queryFn: () => api.getHedgeDashboardBundle(accountId),
        staleTime: STALE_TIME,
        refetchOnMount: "always",
        refetchOnReconnect: true,
        refetchOnWindowFocus: false,
        refetchInterval: 5 * 60_000,
    });
}