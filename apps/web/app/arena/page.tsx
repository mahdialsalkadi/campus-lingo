"use client";

/**
 * /arena — lobby.
 *
 * Rooms are always created from a learning node (the ⚔️ button on the
 * dashboard), because a duel needs a shared, server-gradable question set.
 * This page is the "join with a code my classmate gave me" entry point.
 */

import { Swords } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

export default function ArenaLobbyPage() {
  const router = useRouter();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const join = () => {
    const normalized = code.trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (normalized.length !== 6) {
      setError("كود الغرفة يتكوّن من 6 أحرف/أرقام.");
      return;
    }
    router.push(`/arena/${normalized}`);
  };

  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-5 px-4 py-12 text-center">
      <Swords className="h-12 w-12 text-feather-purple" aria-hidden="true" />
      <div>
        <h1 className="font-display text-2xl font-black text-ink">ساحة المعركة</h1>
        <p className="mt-1 max-w-md font-body text-sm text-ink-soft">
          تحدٍّ مباشر 1 ضد 1 على أسئلة وحدة دراسية. أدخل كود الغرفة الذي أرسله زميلك، أو ابدأ
          مبارزة من زر ⚔️ على أي وحدة في لوحة التحكم.
        </p>
      </div>

      <div className="flex w-full max-w-sm flex-col gap-2">
        <input
          value={code}
          onChange={(event) => setCode(event.target.value.toUpperCase())}
          onKeyDown={(event) => {
            if (event.key === "Enter") join();
          }}
          maxLength={8}
          placeholder="ABC123"
          dir="ltr"
          className="rounded-2xl border-2 border-surface-border bg-surface px-4 py-3 text-center font-display text-xl font-black tracking-[0.3em] text-ink outline-none focus:border-feather-purple"
        />
        {error && (
          <p className="font-body text-xs text-feather-red-dark" dir="auto">
            {error}
          </p>
        )}
        <button
          type="button"
          onClick={join}
          className="rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-6 py-3 font-display text-sm font-black uppercase text-white shadow-key active:translate-y-[2px] active:border-b-0"
        >
          انضم للمعركة
        </button>
      </div>
    </main>
  );
}
