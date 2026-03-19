// src/features/hedge-dashboard/hooks/useHedgeDashboardData.ts
"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
    HedgeIntelligence,
    CrashSimResult,
    HedgeOrderHistoryResponse,
    HedgeHistory,
} from "@/lib/api";

const STALE_TIME = 60_000; // 1 min — hedge data doesn't need sub-second freshness

export function useHedgeIntelligence(accountId = "all") {
    return useQuery<HedgeIntelligence>({
        queryKey: ["hedge-intelligence", accountId],
        queryFn: () => api.getHedgeIntelligence(accountId),
        staleTime: STALE_TIME,
        refetchInterval: 5 * 60_000, // refetch every 5 min
    });
}

export function useCrashSim(accountId = "all") {
    return useQuery<CrashSimResult>({
        queryKey: ["hedge-crash-sim", accountId],
        queryFn: () => api.getCrashSim(accountId),
        staleTime: STALE_TIME,
    });
}

export function useHedgeOrderHistory(state?: string) {
    return useQuery<HedgeOrderHistoryResponse>({
        queryKey: ["hedge-order-history", state ?? "all"],
        queryFn: () => api.getHedgeOrderHistory(state as any),
        staleTime: 30_000,
        refetchInterval: 60_000,
    });
}

export function useHedgeHistory(accountId = "all") {
    // Last 30 days
    const end = new Date().toISOString().slice(0, 10);
    const start = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000)
        .toISOString()
        .slice(0, 10);

    return useQuery<HedgeHistory>({
        queryKey: ["hedge-history", accountId, start, end],
        queryFn: () => api.getHedgeHistory(accountId, start, end),
        staleTime: STALE_TIME,
    });
}