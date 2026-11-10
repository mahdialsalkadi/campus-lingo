"use client";

/**
 * /shop — the Gem Shop.
 *
 * Prices live on the server (`GET /api/shop/items`); this page only renders
 * them and posts a purchase. Effects are applied server-side, so a modified
 * client cannot grant itself a free Streak Freeze.
 */

import { motion } from "framer-motion";
import { Gem, Loader2, ShoppingBag, Sparkles } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type ShopItem } from "@/lib/api";
import { playSfx, useQuizStore } from "@/stores/quizStore";

export default function ShopPage() {
  const userId = useQuizStore((state) => state.userId);
  const gems = useQuizStore((state) => state.gems);
  const ensureUser = useQuizStore((state) => state.ensureUser);
  const refreshStats = useQuizStore((state) => state.refreshStats);

  const [items, setItems] = useState<ShopItem[]>([]);
  const [inventory, setInventory] = useState<Record<string, number | string>>({});
  const [freezeActive, setFreezeActive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [buying, setBuying] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const id = await ensureUser();
      const [{ items: catalog }, stats] = await Promise.all([api.shopItems(), api.stats(id)]);
      setItems(catalog);
      setInventory(stats.inventory ?? {});
      setFreezeActive(Boolean(stats.streak_freeze_active));
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load the shop");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const purchase = useCallback(
    async (item: ShopItem) => {
      if (buying) return;
      setError(null);
      setMessage(null);
      setBuying(item.id);
      try {
        const id = await ensureUser();
        const result = await api.purchaseItem(id, item.id);
        setInventory(result.inventory);
        setFreezeActive(result.streak_freeze_active);
        setMessage(`تم شراء ${item.icon} ${item.name_ar}!`);
        playSfx("fanfare");
        await refreshStats();
      } catch (err) {
        setError(
          err instanceof ApiError && err.status === 402
            ? `تحتاج ${item.price} جوهرة — لديك ${gems}.`
            : err instanceof Error
              ? err.message
              : "تعذّر إتمام الشراء",
        );
        playSfx("incorrect");
      } finally {
        setBuying(null);
      }
    },
    [buying, ensureUser, gems, refreshStats],
  );

  return (
    <main className="flex flex-1 flex-col gap-5 px-4 py-6 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 font-display text-2xl font-black text-ink">
            <ShoppingBag className="h-6 w-6 text-feather-purple" aria-hidden="true" />
            متجر الجواهر
          </h1>
          <p className="mt-0.5 font-body text-sm text-ink-soft">
            اجمع الجواهر من التدريب اليومي وأنفقها على الحمايات والجماليات.
          </p>
        </div>
        <span className="flex items-center gap-1.5 rounded-2xl border-2 border-feather-blue/40 bg-feather-blue/10 px-4 py-2 font-display text-lg font-black text-feather-blue">
          <Gem className="h-5 w-5" aria-hidden="true" />
          {gems}
        </span>
      </header>

      {freezeActive && (
        <p className="flex items-center gap-2 rounded-2xl border-2 border-feather-green bg-feather-green/10 px-4 py-2.5 font-display text-sm font-extrabold text-feather-green-dark">
          🛡️ درع تجميد الحماسة نشط — سلسلتك محمية ليوم غياب واحد.
        </p>
      )}
      {message && (
        <motion.p
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex items-center gap-2 rounded-2xl border-2 border-feather-green bg-feather-green/10 px-4 py-2.5 font-display text-sm font-extrabold text-feather-green-dark"
        >
          <Sparkles className="h-4 w-4" aria-hidden="true" />
          {message}
        </motion.p>
      )}
      {error && (
        <p className="rounded-2xl border-2 border-feather-red bg-feather-red/10 px-4 py-2.5 font-body text-sm text-feather-red-dark">
          {error}
        </p>
      )}

      {loading ? (
        <p className="flex items-center gap-2 font-display text-sm font-extrabold text-ink-soft">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          جارٍ تحميل المتجر…
        </p>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2">
          {items.map((item) => {
            const owned = Number(inventory[item.id] ?? 0);
            const affordable = gems >= item.price;
            return (
              <li
                key={item.id}
                className="flex flex-col gap-3 rounded-3xl border-2 border-surface-border bg-surface p-4 shadow-card"
              >
                <div className="flex items-start gap-3">
                  <span className="text-3xl" aria-hidden="true">
                    {item.icon}
                  </span>
                  <div className="min-w-0 flex-1">
                    <h2 className="font-display text-base font-black text-ink">
                      {item.name_ar}
                      <span className="ms-2 font-body text-[11px] font-bold text-ink-faint">
                        {item.name_en}
                      </span>
                    </h2>
                    <p className="mt-0.5 font-body text-xs text-ink-soft">
                      {item.description_ar}
                    </p>
                  </div>
                </div>
                <div className="mt-auto flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1 font-display text-sm font-black text-feather-blue">
                    <Gem className="h-4 w-4" aria-hidden="true" />
                    {item.price}
                  </span>
                  {owned > 0 && (
                    <span className="rounded-lg bg-surface-muted px-2 py-0.5 font-body text-[11px] font-bold text-ink-faint">
                      تملك {owned}
                    </span>
                  )}
                  <button
                    type="button"
                    disabled={!affordable || buying === item.id}
                    onClick={() => void purchase(item)}
                    className={`flex items-center gap-1.5 rounded-2xl border-2 border-b-4 px-4 py-2 font-display text-xs font-black uppercase transition-all active:translate-y-[2px] active:border-b-0 ${
                      affordable
                        ? "border-feather-green-dark bg-feather-green text-white shadow-key"
                        : "cursor-not-allowed border-surface-border bg-surface-muted text-ink-faint"
                    }`}
                  >
                    {buying === item.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                    ) : null}
                    شراء
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <Link
        href="/leaderboard"
        className="rounded-2xl border-2 border-b-4 border-feather-purple bg-feather-purple/10 px-4 py-3 text-center font-display text-sm font-extrabold text-feather-purple active:translate-y-[2px] active:border-b-0"
      >
        🏆 شاهد دوري هذا الأسبوع
      </Link>
    </main>
  );
}
