"use client";

/**
 * Interactive auth modal: the gate in front of the dashboard.
 *
 * Two flows, one card:
 * - Sign in: POST /api/auth/login verifies email + password (scrypt-hashed
 *   server-side) and restores the account's gems, XP and streak.
 * - Sign up: POST /api/auth/register creates the account with 500 gems.
 *
 * Identity is persisted only after a successful server response — nothing is
 * hardcoded anywhere. Passwords never touch localStorage.
 */

import { AtSign, Eye, EyeOff, Gem, KeyRound, Loader2, LogIn, Sparkles, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";

import { isValidEmail, useQuizStore } from "@/stores/quizStore";

interface AuthModalProps {
  /** Called after a successful sign-in so the caller can load the workspace. */
  onAuthenticated?: (result: { id: string; created: boolean }) => void;
}

type Mode = "signin" | "signup";

export default function AuthModal({ onAuthenticated }: AuthModalProps) {
  const login = useQuizStore((state) => state.login);
  const register = useQuizStore((state) => state.register);
  const storeError = useQuizStore((state) => state.error);
  const clearError = useQuizStore((state) => state.clearError);

  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [pending, setPending] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [welcome, setWelcome] = useState<"none" | "returning" | "new">("none");

  // Surface backend failures (offline, 5xx…) inside the modal.
  useEffect(() => {
    if (storeError) setLocalError(storeError);
  }, [storeError]);

  useEffect(() => () => clearError(), [clearError]);

  const switchMode = (next: Mode) => {
    setMode(next);
    setLocalError(null);
    setPassword("");
    setConfirm("");
  };

  const handleSubmit = async () => {
    const trimmed = email.trim().toLowerCase();
    if (!isValidEmail(trimmed)) {
      setLocalError("أدخل بريد إلكتروني صحيح");
      return;
    }
    if (mode === "signup") {
      if (password.length < 8) {
        setLocalError("كلمة المرور يجب أن تكون 8 أحرف على الأقل");
        return;
      }
      if (password !== confirm) {
        setLocalError("كلمتا المرور غير متطابقتين");
        return;
      }
    } else if (!password) {
      setLocalError("أدخل كلمة المرور");
      return;
    }

    setPending(true);
    setLocalError(null);
    try {
      const result =
        mode === "signup" ? await register(trimmed, password) : await login(trimmed, password);
      setWelcome(result.created ? "new" : "returning");
      setPassword("");
      setConfirm("");
      // Brief welcome flash, then hand over to the dashboard.
      window.setTimeout(() => onAuthenticated?.(result), 900);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "تعذّر تسجيل الدخول");
    } finally {
      setPending(false);
    }
  };

  const inputBase =
    "w-full rounded-xl border-2 border-surface-border bg-surface px-4 py-3 font-body text-sm outline-none focus:border-feather-blue disabled:opacity-60";

  return (
    <main className="flex flex-1 flex-col items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card">
        {welcome === "none" ? (
          <>
            <h1 className="font-display text-center text-2xl font-extrabold text-ink">
              CampusLingo
            </h1>
            <p className="mt-2 text-center font-body text-sm text-ink-soft">
              {mode === "signin"
                ? "سجّل الدخول ببريدك وكلمة المرور — يُستعاد تقدّمك وجواهرك تلقائياً."
                : "أنشئ حساباً ببريدك وكلمة مرور — ونمنحك ٥٠٠ 💎 للبداية."}
            </p>

            {/* Mode tabs ------------------------------------------------ */}
            <div className="mt-4 grid grid-cols-2 gap-1 rounded-2xl bg-surface-muted p-1">
              {(
                [
                  { key: "signin", label: "تسجيل الدخول", icon: LogIn },
                  { key: "signup", label: "حساب جديد", icon: UserPlus },
                ] as const
              ).map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => switchMode(key)}
                  disabled={pending}
                  className={`flex items-center justify-center gap-1.5 rounded-xl px-3 py-2 font-display text-xs font-extrabold uppercase transition-colors ${
                    mode === key
                      ? "bg-surface text-feather-green shadow-sm"
                      : "text-ink-soft hover:text-ink"
                  }`}
                >
                  <Icon className="h-4 w-4" aria-hidden="true" />
                  {label}
                </button>
              ))}
            </div>

            {localError && (
              <p
                role="alert"
                className="mt-3 rounded-xl bg-feather-red/10 px-3 py-2 text-center font-body text-xs text-feather-red-dark"
              >
                {localError}
              </p>
            )}

            <div className="relative mt-4">
              <AtSign
                className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-faint"
                aria-hidden="true"
              />
              <input
                type="email"
                inputMode="email"
                autoComplete="email"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                  if (localError) setLocalError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !pending) void handleSubmit();
                }}
                placeholder="you@university.edu"
                dir="ltr"
                disabled={pending}
                className={`${inputBase} pr-9 text-right`}
                autoFocus
              />
            </div>

            <div className="relative mt-3">
              <KeyRound
                className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-faint"
                aria-hidden="true"
              />
              <input
                type={showPassword ? "text" : "password"}
                autoComplete={mode === "signup" ? "new-password" : "current-password"}
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                  if (localError) setLocalError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !pending) void handleSubmit();
                }}
                placeholder="كلمة المرور"
                dir="ltr"
                disabled={pending}
                minLength={mode === "signup" ? 8 : undefined}
                className={`${inputBase} pr-9 pl-10 text-right`}
              />
              <button
                type="button"
                onClick={() => setShowPassword((value) => !value)}
                tabIndex={-1}
                aria-label={showPassword ? "إخفاء كلمة المرور" : "إظهار كلمة المرور"}
                className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint hover:text-ink-soft"
              >
                {showPassword ? (
                  <EyeOff className="h-4 w-4" aria-hidden="true" />
                ) : (
                  <Eye className="h-4 w-4" aria-hidden="true" />
                )}
              </button>
            </div>

            {mode === "signup" && (
              <input
                type={showPassword ? "text" : "password"}
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => {
                  setConfirm(event.target.value);
                  if (localError) setLocalError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !pending) void handleSubmit();
                }}
                placeholder="تأكيد كلمة المرور"
                dir="ltr"
                disabled={pending}
                className={`${inputBase} mt-3 text-right`}
              />
            )}

            <button
              type="button"
              onClick={() => void handleSubmit()}
              disabled={pending}
              className="mt-4 w-full rounded-xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-4 py-3 font-display text-sm font-extrabold uppercase text-white shadow-key active:translate-y-[2px] disabled:opacity-50"
            >
              {pending ? (
                <span className="flex items-center justify-center gap-2">
                  <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
                  جارٍ المعالجة…
                </span>
              ) : (
                <span className="flex items-center justify-center gap-2">
                  {mode === "signin" ? (
                    <LogIn className="h-5 w-5" aria-hidden="true" />
                  ) : (
                    <UserPlus className="h-5 w-5" aria-hidden="true" />
                  )}
                  {mode === "signin" ? "دخول" : "إنشاء الحساب"}
                </span>
              )}
            </button>

            <p className="mt-4 text-center font-body text-[11px] leading-relaxed text-ink-faint">
              {mode === "signin"
                ? "ليس لديك حساب؟ انتقل إلى «حساب جديد» بالأعلى — نمنحك ٥٠٠ 💎 للبداية"
                : "لديك حساب بالفعل؟ انتقل إلى «تسجيل الدخول» بالأعلى لاستعادة تقدّمك"}
            </p>
          </>
        ) : (
          <div className="flex flex-col items-center gap-3 py-4 text-center">
            <Sparkles className="h-10 w-10 text-feather-green" aria-hidden="true" />
            <p className="font-display text-xl font-extrabold text-ink">
              {welcome === "new" ? "مرحباً بك! 🎉" : "أهلاً بعودتك!"}
            </p>
            <p className="flex items-center gap-1 font-body text-sm text-ink-soft">
              <Gem className="h-4 w-4 text-feather-blue" aria-hidden="true" />
              {welcome === "new" ? "حسابك جاهز بـ 500 جوهرة" : "تم استعادة تقدّمك"}
            </p>
            <Loader2 className="mt-2 h-5 w-5 animate-spin text-feather-blue" aria-hidden="true" />
          </div>
        )}
      </div>
    </main>
  );
}
