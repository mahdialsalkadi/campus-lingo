"use client";

/**
 * Profile & learning analytics: identity card, gamification stats, an FSRS
 * memory gauge, and the Mistake Notebook — a carousel of recently lapsed
 * cards with one-tap launch into a rapid-fire review session.
 */

import {
  Bell,
  BrickWall,
  Crown,
  Flame,
  Gem,
  GraduationCap,
  LogOut,
  PlayCircle,
  Sparkles,
  Target,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  CHALLENGE_ICONS,
  CHALLENGE_LABELS,
  LEAGUE_NAMES_FALLBACK,
  api,
  type Entitlements,
  type NotificationItem,
  type QueueItem,
  type UserStats,
} from "@/lib/api";
import { isStaleIdentity, useQuizStore } from "@/stores/quizStore";

/** Feature-detect the Web Push API before offering the switch. */
function supportsWebPush(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** base64url VAPID key -> Uint8Array for pushManager.subscribe. */
function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = window.atob(normalized);
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

/** Deterministic pastel pair from the email — no external avatar service. */
function avatarHues(email: string): [number, number] {
  let hash = 0;
  for (const char of email) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return [hash % 360, (hash / 360) % 360];
}

/** FSRS memory strength: average retrievability of due cards (0..1). */
function memoryGauge(items: QueueItem[]): number {
  const usable = items.filter((item) => item.retrievability != null);
  if (usable.length === 0) return 1; // nothing at risk — memory fully healthy
  return usable.reduce((sum, item) => sum + (item.retrievability ?? 0), 0) / usable.length;
}

export default function ProfilePage() {
  const router = useRouter();
  const userId = useQuizStore((state) => state.userId);
  const email = useQuizStore((state) => state.email);
  const xp = useQuizStore((state) => state.xp);
  const gems = useQuizStore((state) => state.gems);
  const streak = useQuizStore((state) => state.streak);
  const leagueTier = useQuizStore((state) => state.leagueTier);
  const logout = useQuizStore((state) => state.logout);
  const startQueueSession = useQuizStore((state) => state.startQueueSession);

  const [stats, setStats] = useState<UserStats | null>(null);
  const [mistakes, setMistakes] = useState<QueueItem[]>([]);
  const [courses, setCourses] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [entitlements, setEntitlements] = useState<Entitlements | null>(null);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [proOpen, setProOpen] = useState(false);
  const [proBusy, setProBusy] = useState(false);
  const [pushState, setPushState] = useState<"unknown" | "off" | "on" | "unsupported">("off");

  /** Pro Pass checkout + entitlement refresh (the provider webhook does this). */
  const activatePro = useCallback(async () => {
    if (!userId || proBusy) return;
    setProBusy(true);
    setError(null);
    try {
      const body = await api.activatePro(userId, 1, "checkout_hook");
      setEntitlements(body);
      setProOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "تعذّر تفعيل الاشتراك");
    } finally {
      setProBusy(false);
    }
  }, [proBusy, userId]);

  /**
   * Enable retention alerts: register the service worker (already done at boot)
   * and hand the browser's push endpoint to the server. Web Push delivery needs
   * VAPID keys + pywebpush on the API, so an unconfigured server is reported
   * honestly instead of silently pretending the alerts are live.
   */
  const enablePush = useCallback(async () => {
    if (!userId) return;
    if (!supportsWebPush()) {
      setPushState("unsupported");
      return;
    }
    try {
      const key = await api.pushPublicKey();
      if (!key.public_key) {
        setError("إشعارات المتصفح غير مهيّأة على الخادم (VAPID).");
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setError("تم رفض إذن الإشعارات من المتصفح.");
        return;
      }
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key.public_key),
      });
      await api.pushSubscribe(userId, subscription.toJSON(), navigator.userAgent);
      setPushState("on");
    } catch (err) {
      setPushState("off");
      setError(err instanceof Error ? err.message : "تعذّر تفعيل الإشعارات");
    }
  }, [userId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!userId) return;
      try {
        const [userStats, mistakeBag, courseList, entitlementsBody, inbox] = await Promise.all([
          api.stats(userId),
          api.mistakes(userId, 50),
          api.listCourses(userId),
          api.entitlements(userId),
          api.notifications(userId, 12),
        ]);
        if (cancelled) return;
        setStats(userStats);
        setMistakes(mistakeBag.items);
        setCourses(courseList.length);
        setEntitlements(entitlementsBody);
        setNotifications(inbox.items);
        setPushState(supportsWebPush() ? "off" : "unsupported");
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "unexpected error");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [userId]);

  /** Rapid-fire review: every failed card, right now. */
  const launchWeakSpots = useCallback(() => {
    if (mistakes.length === 0) return;
    startQueueSession(mistakes);
    router.push("/play/review");
  }, [mistakes, startQueueSession, router]);

  const retention = useMemo(() => memoryGauge(mistakes), [mistakes]);
  const [hueA, hueB] = avatarHues(email || "student");
  const retentionColor =
    retention >= 0.8 ? "var(--cl-green)" : retention >= 0.55 ? "var(--cl-orange)" : "var(--cl-red)";

  if (loading) {
    return (
      <main className="flex flex-1 items-center justify-center px-4 py-16">
        <p className="animate-pulse font-display text-lg font-extrabold text-ink-soft">
          جارٍ تحميل ملفك…
        </p>
      </main>
    );
  }

  return (
    <main className="flex flex-1 flex-col gap-5 px-4 py-6 sm:px-6">
      {error && (
        <p className="rounded-2xl border-2 border-feather-red bg-feather-red/10 px-4 py-3 font-body text-sm text-feather-red-dark">
          {error}
        </p>
      )}

      {/* Identity card ------------------------------------------------------ */}
      <section className="flex flex-wrap items-center gap-4 rounded-3xl border-2 border-surface-border bg-surface p-5 shadow-card">
        <span
          className="flex h-16 w-16 items-center justify-center rounded-full font-display text-2xl font-black text-white"
          style={{
            background: `linear-gradient(135deg, hsl(${hueA} 70% 55%), hsl(${hueB} 70% 45%))`,
          }}
          aria-hidden="true"
        >
          {(email || "S").trim().charAt(0).toUpperCase()}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-display text-lg font-black text-ink" dir="ltr">
            {email || "—"}
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className="rounded-full bg-feather-purple/15 px-2.5 py-0.5 font-display text-[10px] font-extrabold uppercase tracking-wider text-feather-purple">
              {LEAGUE_NAMES_FALLBACK[Math.min(leagueTier, LEAGUE_NAMES_FALLBACK.length - 1)]} League
            </span>
            {isStaleIdentity(email) && (
              <span className="rounded-full bg-feather-orange/15 px-2.5 py-0.5 font-display text-[10px] font-extrabold uppercase text-feather-orange">
                حساب قديم
              </span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={() => {
            logout();
            router.push("/");
          }}
          className="flex items-center gap-2 rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-4 py-2.5 font-display text-xs font-extrabold uppercase tracking-wider text-ink-soft transition-colors hover:border-feather-red hover:text-feather-red active:translate-y-[2px] active:border-b-0"
        >
          <LogOut className="h-4 w-4" aria-hidden="true" />
          تبديل الحساب
        </button>
      </section>

      {/* Creator royalties + Pro Pass ---------------------------------------- */}
      <section className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-2 rounded-3xl border-2 border-feather-yellow/50 bg-feather-yellow/10 p-4 shadow-card">
          <p className="flex items-center gap-2 font-display text-sm font-extrabold text-ink">
            <Crown className="h-4 w-4 text-feather-yellow" aria-hidden="true" />
            أرباح المُنشئ
          </p>
          <p className="font-display text-3xl font-black text-ink">
            {entitlements?.creator_gems ?? 0}{" "}
            <span className="font-body text-sm font-bold text-ink-faint">💎 جوهرة</span>
          </p>
          <p className="font-body text-[11px] text-ink-soft">
            تُضاف جوهرتان تلقائياً كلما أكمل زميل تحدياً في مقرر من مشاركتك.
          </p>
        </div>

        <div className="flex flex-col gap-2 rounded-3xl border-2 border-feather-purple/40 bg-feather-purple/5 p-4 shadow-card">
          <p className="flex items-center gap-2 font-display text-sm font-extrabold text-ink">
            <Sparkles className="h-4 w-4 text-feather-purple" aria-hidden="true" />
            تمريرة Pro
          </p>
          {entitlements?.pro_active ? (
            <>
              <p className="font-display text-lg font-black text-feather-purple">نشطة ✓</p>
              <p className="font-body text-[11px] text-ink-soft">
                بلا إعلانات · أولوية معالجة · أوراق مراجعة بدون اتصال
              </p>
            </>
          ) : (
            <>
              <p className="font-display text-lg font-black text-ink">
                {(entitlements?.price_usd_cents ?? 100) / 100}$ / شهر
              </p>
              <p className="font-body text-[11px] text-ink-soft">
                بلا إعلانات، رفع غير محدود، وتحميل أوراق المراجعة دون اتصال.
              </p>
              <button
                type="button"
                onClick={() => setProOpen(true)}
                className="mt-1 rounded-2xl border-2 border-b-4 border-feather-purple bg-feather-purple px-4 py-2 font-display text-xs font-extrabold text-white active:translate-y-[2px] active:border-b-2"
              >
                ترقية إلى Pro
              </button>
            </>
          )}
        </div>
      </section>

      {/* Stats grid ---------------------------------------------------------- */}
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { icon: Sparkles, label: "إجمالي XP", value: xp, tone: "text-feather-purple" },
          { icon: Flame, label: "سلسلة الأيام", value: streak, tone: "text-feather-orange" },
          {
            icon: Target,
            label: "بطاقات FSRS",
            value: stats?.cards_total ?? 0,
            tone: "text-feather-green",
          },
          { icon: Gem, label: "المقررات", value: courses, tone: "text-feather-blue" },
        ].map(({ icon: Icon, label, value, tone }) => (
          <div
            key={label}
            className="flex flex-col items-center gap-1 rounded-3xl border-2 border-surface-border bg-surface p-4 shadow-card"
          >
            <Icon className={`h-5 w-5 ${tone}`} aria-hidden="true" />
            <p className="font-display text-2xl font-black text-ink">{value}</p>
            <p className="font-body text-[11px] text-ink-faint">{label}</p>
          </div>
        ))}
      </section>

      {/* Retention alerts + inbox -------------------------------------------- */}
      <section className="rounded-3xl border-2 border-surface-border bg-surface p-5 shadow-card">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-sm font-extrabold text-ink">
            <Bell className="h-4 w-4 text-feather-blue" aria-hidden="true" />
            التنبيهات والإشعارات
          </h2>
          <button
            type="button"
            disabled={pushState === "unsupported"}
            onClick={() => void enablePush()}
            className={`rounded-2xl border-2 border-b-4 px-3 py-1.5 font-display text-[11px] font-extrabold transition-all active:translate-y-[2px] active:border-b-2 disabled:opacity-40 ${
              pushState === "on"
                ? "border-feather-green-dark bg-feather-green text-white"
                : "border-surface-border bg-surface text-ink-soft"
            }`}
          >
            {pushState === "on"
              ? "إشعارات المتصفح مفعّلة ✓"
              : pushState === "unsupported"
                ? "المتصفح لا يدعم الإشعارات"
                : "تفعيل إشعارات المتصفح"}
          </button>
        </div>
        {notifications.length === 0 ? (
          <p className="py-3 text-center font-body text-xs text-ink-faint">
            لا تنبيهات بعد — سنخبرك عند نشر مادة جديدة أو قبل انكسار سلسلتك.
          </p>
        ) : (
          <ul className="flex flex-col divide-y-2 divide-surface-border">
            {notifications.map((item) => (
              <li key={item.id} className="flex items-start justify-between gap-3 py-2.5">
                <div className="min-w-0">
                  <p className="font-body text-xs font-bold text-ink" dir="auto">
                    {item.title}
                  </p>
                  <p className="font-body text-[11px] text-ink-faint" dir="auto">
                    {item.body}
                  </p>
                </div>
                <span className="shrink-0 rounded-lg bg-surface-muted px-1.5 py-0.5 font-body text-[9px] font-bold uppercase text-ink-faint">
                  {item.push_status.includes("sent") ? "push" : "داخلي"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Memory retention gauge ---------------------------------------------- */}
      <section className="flex flex-wrap items-center gap-5 rounded-3xl border-2 border-surface-border bg-surface p-5 shadow-card">
        <svg viewBox="0 0 84 84" className="h-24 w-24 shrink-0 -rotate-90">
          <circle cx="42" cy="42" r="36" fill="none" stroke="var(--cl-surface-border)" strokeWidth="9" />
          <circle
            cx="42"
            cy="42"
            r="36"
            fill="none"
            stroke={retentionColor}
            strokeWidth="9"
            strokeLinecap="round"
            strokeDasharray={2 * Math.PI * 36}
            strokeDashoffset={2 * Math.PI * 36 * (1 - retention)}
            className="ring-draw"
            style={{ "--ring-circumference": 2 * Math.PI * 36 } as React.CSSProperties}
          />
        </svg>
        <div className="min-w-0 flex-1">
          <h2 className="flex items-center gap-2 font-display text-base font-black text-ink">
            <GraduationCap className="h-5 w-5 text-feather-blue" aria-hidden="true" />
            قوة الذاكرة (FSRS-4.5)
          </h2>
          <p className="mt-1 font-body text-sm text-ink-soft">
            {Math.round(retention * 100)}% استرجاع متوقع · {stats?.cards_due ?? 0} بطاقة مستحقة ·{" "}
            {stats?.cards_relearning ?? 0} قيد إعادة التعلّم
          </p>
          <div className="mt-2 h-2.5 overflow-hidden rounded-full bg-surface-border">
            <div
              className="h-full rounded-full transition-all"
              style={{ width: `${Math.round(retention * 100)}%`, background: retentionColor }}
            />
          </div>
        </div>
      </section>

      {/* Mistake notebook ------------------------------------------------------ */}
      <section className="flex flex-col gap-3 rounded-3xl border-2 border-surface-border bg-surface p-5 shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 font-display text-base font-black text-ink">
            <BrickWall className="h-5 w-5 text-feather-red" aria-hidden="true" />
            دفتر الأخطاء
          </h2>
          <button
            type="button"
            disabled={mistakes.length === 0}
            onClick={launchWeakSpots}
            className="flex items-center gap-2 rounded-2xl border-2 border-b-4 border-feather-red-dark bg-feather-red px-4 py-2 font-display text-xs font-black uppercase tracking-wider text-white shadow-key active:translate-y-[2px] active:border-b-0 disabled:opacity-50"
          >
            <PlayCircle className="h-4 w-4" aria-hidden="true" />
            نقّف نقاط الضعف ({mistakes.length})
          </button>
        </div>

        {mistakes.length === 0 ? (
          <p className="rounded-2xl border-2 border-dashed border-surface-border bg-surface-muted p-4 text-center font-body text-sm text-ink-soft">
            لا أخطاء مسجّلة — ذاكرتك بمأمن من الحذف 🎉
          </p>
        ) : (
          <div className="flex snap-x gap-3 overflow-x-auto pb-2">
            {mistakes.slice(0, 12).map((item) => (
              <article
                key={item.card_id}
                className="w-64 shrink-0 snap-start rounded-2xl border-2 border-surface-border bg-surface-muted p-3"
              >
                <div className="flex items-center justify-between font-display text-[10px] font-extrabold uppercase text-ink-faint">
                  <span className="flex items-center gap-1">
                    {CHALLENGE_ICONS[item.type] ?? "•"}{" "}
                    {CHALLENGE_LABELS[item.type]?.ar ?? item.type}
                  </span>
                  <span className="text-feather-red">{item.lapses} خطأ</span>
                </div>
                <p className="mt-1.5 line-clamp-2 font-body text-xs font-bold text-ink" dir="auto">
                  {item.node_title}
                </p>
                <p className="mt-1 font-body text-[11px] text-ink-faint">
                  الاستقرار {item.stability.toFixed(1)} · صعوبة {item.difficulty.toFixed(1)}
                  {item.retrievability != null &&
                    ` · تذكّر ${Math.round(item.retrievability * 100)}%`}
                </p>
              </article>
            ))}
          </div>
        )}
        {mistakes.length > 0 && (
          <p className="font-body text-[11px] text-ink-faint">
            كل البطاقات الخاطئة تعود تلقائيًا عبر جدول FSRS-4.5 قبل أن تتلاشى من الذاكرة
          </p>
        )}
      </section>

      {/* $1 Pro Pass upgrade modal ------------------------------------------ */}
      {proOpen && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-4 sm:items-center"
          role="dialog"
          aria-modal="true"
          onClick={() => setProOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-3xl border-2 border-surface-border bg-surface p-5 shadow-card"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 className="mb-1 flex items-center gap-2 font-display text-lg font-black text-ink">
              <Crown className="h-5 w-5 text-feather-yellow" aria-hidden="true" />
              تمريرة Pro — {(entitlements?.price_usd_cents ?? 100) / 100}$ / شهر
            </h2>
            <ul className="my-3 flex flex-col gap-2 font-body text-xs text-ink-soft">
              <li>🚫 بلا أي إعلانات داخل التطبيق</li>
              <li>⚡ أولوية في معالجة المحاضرات</li>
              <li>📄 تحميل أوراق المراجعة للاستخدام دون اتصال</li>
            </ul>
            <p className="mb-4 rounded-2xl bg-surface-muted p-2.5 font-body text-[10px] text-ink-faint">
              التفعيل يتم عبر بوابة الدفع (Stripe / Play Billing) ثم تُستدعى خدمة
              الاستحقاقات. هذا الزر يحاكي عودة البوابة بعد نجاح الدفع.
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setProOpen(false)}
                className="flex-1 rounded-2xl border-2 border-surface-border bg-surface px-4 py-2.5 font-display text-xs font-extrabold text-ink-soft"
              >
                لاحقاً
              </button>
              <button
                type="button"
                disabled={proBusy}
                onClick={() => void activatePro()}
                className="flex-1 rounded-2xl border-2 border-b-4 border-feather-purple bg-feather-purple px-4 py-2.5 font-display text-xs font-extrabold text-white disabled:opacity-50 active:translate-y-[2px] active:border-b-2"
              >
                {proBusy ? "جارٍ التفعيل…" : "تفعيل الآن"}
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
