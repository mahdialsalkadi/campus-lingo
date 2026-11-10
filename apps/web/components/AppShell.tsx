"use client";

/**
 * App shell chrome: desktop sidebar + mobile bottom nav + sticky top status
 * bar. Extracted from the dashboard so /profile and future routes share one
 * navigation framework (the Duolingo/Linear shell).
 */

import {
  Gauge,
  GraduationCap,
  Home,
  RefreshCw,
  Repeat,
  ShoppingBag,
  Sparkles,
  Trophy,
  User,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import StreakCalendar from "@/components/StreakCalendar";
import ThemeToggle from "@/components/ThemeToggle";
import { MODEL_TIERS, api, type ModelTierId } from "@/lib/api";
import { useQuizStore } from "@/stores/quizStore";

const NAV_ITEMS: {
  href: string;
  label: string;
  labelEn: string;
  icon: typeof Home;
  match?: string;
}[] = [
  { href: "/", label: "تعلّم", labelEn: "Learn", icon: Home },
  { href: "/play/review", label: "مراجعة", labelEn: "Review", icon: Repeat, match: "/play/review" },
  { href: "/leaderboard", label: "الدوري", labelEn: "League", icon: Trophy },
  { href: "/shop", label: "المتجر", labelEn: "Shop", icon: ShoppingBag },
  { href: "/profile", label: "حسابي", labelEn: "Profile", icon: User },
];

function useOnlineTiers(): Set<string> {
  const [online, setOnline] = useState<Set<string>>(new Set());
  useEffect(() => {
    let cancelled = false;
    api
      .health()
      .then((body) => {
        if (cancelled) return;
        const tiers = body.llm?.tiers ?? {};
        setOnline(
          new Set(
            Object.entries(tiers)
              .filter(([, status]) => status.ok)
              .map(([id]) => id),
          ),
        );
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  return online;
}

/** Pill showing which Qwen tier is serving (green = online). */
function TierPill() {
  const online = useOnlineTiers();
  const active = MODEL_TIERS.find((tier) => online.has(tier.id));
  return (
    <span
      className={`flex items-center gap-1.5 rounded-full border-2 px-2.5 py-1 font-display text-[10px] font-extrabold uppercase tracking-wider ${
        active
          ? "border-feather-green/40 bg-feather-green/10 text-feather-green-dark"
          : "border-surface-border bg-surface-muted text-ink-faint"
      }`}
      title={active ? `${active.name} متصل` : "لا يوجد خادم نموذج متصل"}
    >
      <Sparkles className="h-3 w-3" aria-hidden="true" />
      {active ? active.id : "offline"}
    </span>
  );
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const xp = useQuizStore((state) => state.xp);
  const gems = useQuizStore((state) => state.gems);
  const email = useQuizStore((state) => state.email);
  const offlinePending = useQuizStore((state) => state.offlinePending);
  const syncOfflineReviews = useQuizStore((state) => state.syncOfflineReviews);

  // Offline sync: replay reviews queued during airplane-mode play the moment
  // connectivity returns (and once on mount, in case the tab reopens online).
  useEffect(() => {
    const flush = () => {
      void syncOfflineReviews();
    };
    if (typeof navigator === "undefined" || navigator.onLine) flush();
    window.addEventListener("online", flush);
    return () => window.removeEventListener("online", flush);
  }, [syncOfflineReviews]);
  // Immersive routes (quiz session, live duel): no shell chrome at all.
  const isArena =
    (pathname.startsWith("/play/") && pathname !== "/play/review") ||
    pathname.startsWith("/arena/");

  const navItems = NAV_ITEMS.map((item) => {
    const active =
      item.href === "/" ? pathname === "/" : pathname.startsWith(item.match ?? item.href);
    return { ...item, active };
  });

  const navList = (vertical: boolean) => (
    <nav className={vertical ? "flex flex-col gap-1.5" : "flex flex-1 justify-around"}>
      {navItems.map((item) => {
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            className={
              vertical
                ? `flex items-center gap-3 rounded-2xl px-3 py-2.5 font-display text-sm font-extrabold transition-colors ${
                    item.active
                      ? "bg-feather-blue/10 text-feather-blue-dark"
                      : "text-ink-soft hover:bg-surface-muted hover:text-ink"
                  }`
                : `nav-pop flex flex-col items-center gap-0.5 rounded-2xl px-3 py-1.5 font-display text-[10px] font-extrabold transition-colors ${
                    item.active ? "text-feather-blue-dark" : "text-ink-faint"
                  }`
            }
          >
            <Icon
              className={`${vertical ? "h-5 w-5" : "h-6 w-6"} ${item.active ? "nav-pop" : ""}`}
              aria-hidden="true"
            />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );

  if (isArena) {
    return <>{children}</>;
  }

  return (
    <>
      {/* Desktop sidebar (Linear-style) ------------------------------------ */}
      <aside className="fixed inset-y-0 start-0 z-40 hidden w-60 flex-col gap-4 border-e-2 border-surface-border bg-surface px-4 py-5 lg:flex">
        <div className="flex items-center gap-2 px-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-2xl bg-feather-green font-display text-lg font-black text-white shadow-key-sm">
            L
          </span>
          <span className="font-display text-lg font-black text-ink">Lingo</span>
        </div>
        {navList(true)}
        <div className="mt-auto flex flex-col gap-3 rounded-2xl border-2 border-surface-border bg-surface-muted p-3">
          <div className="flex items-center gap-2 font-display text-xs font-extrabold text-ink">
            <Gauge className="h-4 w-4 text-feather-purple" aria-hidden="true" />
            <span className="truncate" dir="ltr">
              {email || "guest"}
            </span>
          </div>
          <div className="flex items-center justify-between font-display text-xs font-bold text-ink-soft">
            <span className="flex items-center gap-1 text-feather-purple">
              <Sparkles className="h-3.5 w-3.5" aria-hidden="true" /> {xp} XP
            </span>
            <span className="text-feather-blue">💎 {gems}</span>
          </div>
          {offlinePending > 0 && (
            <span className="flex items-center gap-1 font-display text-[10px] font-extrabold text-ink-faint">
              <RefreshCw className="h-3 w-3" aria-hidden="true" />
              {offlinePending} بانتظار المزامنة
            </span>
          )}
          <ThemeToggle />
        </div>
      </aside>

      {/* Mobile top sticky bar --------------------------------------------- */}
      <header className="sticky top-0 z-40 flex items-center justify-between gap-2 border-b-2 border-surface-border bg-surface/95 px-4 py-2 backdrop-blur lg:hidden">
        <Link href="/" className="flex items-center gap-1.5">
          <GraduationCap className="h-5 w-5 text-feather-green" aria-hidden="true" />
          <span className="font-display text-base font-black text-ink">Lingo</span>
        </Link>
        <div className="flex items-center gap-2">
          {offlinePending > 0 && (
            <span
              className="flex items-center gap-1 rounded-full border-2 border-feather-yellow/50 bg-feather-yellow/15 px-2 py-0.5 font-display text-[10px] font-extrabold text-ink"
              title={`${offlinePending} مراجعة بانتظار المزامنة`}
            >
              <RefreshCw className="h-3 w-3" aria-hidden="true" /> {offlinePending}
            </span>
          )}
          <StreakCalendar compact />
          <span className="flex items-center gap-1 font-display text-sm font-extrabold text-feather-purple">
            <Sparkles className="h-4 w-4" aria-hidden="true" />
            {xp}
          </span>
          <span className="flex items-center gap-1 font-display text-sm font-extrabold text-feather-blue">
            💎 {gems}
          </span>
          <TierPill />
        </div>
      </header>

      {/* Page canvas: offset for sidebar (desktop) and bottom bar (mobile) -- */}
      <div className="lg:ps-60">
        <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col pb-20 lg:pb-0">
          {children}
        </div>
      </div>

      {/* Mobile bottom nav --------------------------------------------------- */}
      <nav className="fixed inset-x-0 bottom-0 z-40 flex items-center justify-around border-t-2 border-surface-border bg-surface/95 px-2 py-1.5 backdrop-blur lg:hidden">
        {navList(false)}
      </nav>
    </>
  );
}
