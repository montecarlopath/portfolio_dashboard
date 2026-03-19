// src/app/hedge/page.tsx
import type { Metadata } from "next";
import { HedgeDashboard } from "@/features/hedge-dashboard/components/HedgeDashboard";

export const metadata: Metadata = {
    title: "Hedge Dashboard",
};

export default function HedgePage() {
    return <HedgeDashboard />;
}

