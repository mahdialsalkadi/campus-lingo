"use client";

/**
 * /arena/[roomCode] — the live 1v1 Battle Arena.
 *
 * One WebSocket drives the whole screen: `question` frames reset the local
 * 60-second countdown, `state` frames redraw the racing progress bars, and
 * `finish` reveals the podium and claims the victor's reward server-side.
 *
 * The score itself is never computed here — the server grades every answer
 * (accuracy + seconds remaining) so a modified client cannot inflate a duel.
 */

import confetti from "canvas-confetti";
import { motion } from "framer-motion";
import { Bot, Check, Copy, Crown, Flame, LogOut, Swords, Trophy, Zap } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  arenaSocketUrl,
  api,
  type ArenaEvent,
  type ArenaPlayerState,
  type ArenaQuestion,
} from "@/lib/api";
import { playSfx, useQuizStore } from "@/stores/quizStore";

const QUESTION_SECONDS_FALLBACK = 60;

export default function ArenaPage({ params }: { params: { roomCode: string } }) {
  const roomCode = params.roomCode.toUpperCase();
  const router = useRouter();
  const userId = useQuizStore((state) => state.userId);
  const email = useQuizStore((state) => state.email);
  const ensureUser = useQuizStore((state) => state.ensureUser);
  const refreshStats = useQuizStore((state) => state.refreshStats);

  const [status, setStatus] = useState<"connecting" | "waiting" | "playing" | "finished" | "error">(
    "connecting",
  );
  const [myId, setMyId] = useState<string | null>(null);
  const [players, setPlayers] = useState<ArenaPlayerState[]>([]);
  const [question, setQuestion] = useState<ArenaQuestion | null>(null);
  const [index, setIndex] = useState(0);
  const [total, setTotal] = useState(0);
  const [secondsLeft, setSecondsLeft] = useState(QUESTION_SECONDS_FALLBACK);
  const [selected, setSelected] = useState<number | null>(null);
  const [reveal, setReveal] = useState<number | null>(null);
  const [combo, setCombo] = useState(0);
  const [standings, setStandings] = useState<
    { player_id: string; name: string; score: number }[]
  >([]);
  const [reward, setReward] = useState<{ xp: number; gems: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [leavePrompt, setLeavePrompt] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const rewardedRef = useRef(false);

  const me = players.find((player) => player.player_id === myId) ?? null;
  const rival = players.find((player) => player.player_id !== myId) ?? null;
  const won = standings.length > 0 && standings[0]?.player_id === myId;

  // -- socket lifecycle ------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    let socket: WebSocket | null = null;

    (async () => {
      const id = userId ?? (await ensureUser());
      if (cancelled) return;
      const name = email || "أنت";
      socket = new WebSocket(arenaSocketUrl(roomCode, id, name));
      socketRef.current = socket;

      socket.onmessage = (event) => {
        if (cancelled) return;
        let message: ArenaEvent;
        try {
          message = JSON.parse(event.data as string) as ArenaEvent;
        } catch {
          return;
        }
        switch (message.type) {
          case "welcome":
            setMyId(message.player_id);
            setStatus("waiting");
            break;
          case "start":
            setStatus("playing");
            setTotal(message.total);
            break;
          case "question":
            setQuestion(message.question);
            setIndex(message.index);
            setTotal(message.total);
            setSelected(null);
            setReveal(null);
            setSecondsLeft(message.seconds || QUESTION_SECONDS_FALLBACK);
            setStatus("playing");
            playSfx("whoosh");
            break;
          case "state":
            setPlayers(message.players);
            break;
          case "answer_ack":
            if (message.correct) {
              playSfx("correct");
              setCombo((previous) => previous + 1);
            } else {
              playSfx("incorrect");
              setCombo(0);
            }
            break;
          case "reveal":
            setReveal(message.answer_index);
            break;
          case "finish":
            setStandings(message.standings);
            setStatus("finished");
            if (message.winner === id) {
              playSfx("fanfare");
              void confetti({ particleCount: 180, spread: 90, origin: { y: 0.6 } });
            }
            break;
          case "error":
            setError(message.message);
            setStatus("error");
            break;
          default:
            break;
        }
      };
      socket.onerror = () => {
        if (!cancelled) {
          setError("تعذّر الاتصال بغرفة المعركة.");
          setStatus("error");
        }
      };
      socket.onclose = () => {
        if (!cancelled && status === "connecting") {
          setError("الغرفة غير موجودة أو انتهت الجلسة.");
          setStatus("error");
        }
      };
    })();

    return () => {
      cancelled = true;
      socket?.close();
      socketRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roomCode]);

  // -- local countdown (server value is authoritative per question) ----------
  useEffect(() => {
    if (status !== "playing" || reveal !== null) return;
    const timer = setInterval(() => {
      setSecondsLeft((previous) => (previous > 0 ? previous - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [status, reveal, index]);

  // -- room code copy + graceful exit ----------------------------------------
  const copyRoomCode = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(roomCode);
    } catch {
      // Clipboard API can be unavailable (http, permissions) — fall back.
      const area = document.createElement("textarea");
      area.value = roomCode;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }, [roomCode]);

  /** Close the WebSocket with Redis gracefully, then return to the dashboard. */
  const leaveArena = useCallback(() => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      try {
        socket.close(1000, "left arena");
      } catch {
        /* the effect cleanup closes it again if needed */
      }
    }
    socketRef.current = null;
    setLeavePrompt(false);
    router.push("/");
  }, [router]);

  const answer = useCallback(
    (option: number) => {
      if (selected !== null || reveal !== null) return;
      setSelected(option);
      playSfx("tap");
      socketRef.current?.send(JSON.stringify({ type: "answer", selected: option }));
    },
    [reveal, selected],
  );

  // -- reward the victor once ------------------------------------------------
  useEffect(() => {
    if (status !== "finished" || !myId || !won || rewardedRef.current) return;
    rewardedRef.current = true;
    void (async () => {
      try {
        const result = await api.arenaReward(roomCode, myId);
        if (result.awarded) {
          setReward({ xp: result.xp ?? 100, gems: result.gems ?? 25 });
          await refreshStats();
        }
      } catch {
        /* podium still shows; the payout can be retried by refreshing */
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, myId, won, roomCode]);

  const progressFor = useMemo(
    () => (player: ArenaPlayerState | null) =>
      total === 0 ? 0 : Math.min(100, Math.round((player?.score ?? 0) / Math.max(1, total * 100) * 100)),
    [total],
  );

  // -- screens ---------------------------------------------------------------
  if (error) {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-4 px-4 text-center">
        <p className="rounded-2xl border-2 border-feather-red bg-feather-red/10 px-4 py-3 font-body text-sm text-feather-red-dark">
          {error}
        </p>
        <Link
          href="/"
          className="rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-5 py-3 font-display text-sm font-extrabold uppercase text-white shadow-key"
        >
          لوحة التحكم
        </Link>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen flex-col bg-gradient-to-b from-surface to-surface-muted px-4 py-5">
      <header className="sticky top-0 z-20 -mx-4 mb-1 flex items-center justify-between gap-3 border-b-2 border-surface-border bg-surface/95 px-4 py-2.5 backdrop-blur">
        <button
          type="button"
          onClick={() => void copyRoomCode()}
          className="flex items-center gap-2 rounded-xl border-2 border-surface-border bg-surface-muted px-2.5 py-1.5 font-display text-sm font-black text-ink transition-colors hover:border-feather-purple/60"
          title="نسخ كود الغرفة"
        >
          <Swords className="h-4 w-4 text-feather-purple" aria-hidden="true" />
          <span dir="ltr">{roomCode}</span>
          {copied ? (
            <Check className="h-4 w-4 text-feather-green" aria-hidden="true" />
          ) : (
            <Copy className="h-4 w-4 text-ink-faint" aria-hidden="true" />
          )}
        </button>
        <span className="rounded-xl bg-ink/5 px-2.5 py-1 font-display text-xs font-extrabold text-ink-soft">
          {index + 1} / {total || "—"}
        </span>
        <button
          type="button"
          onClick={() => setLeavePrompt(true)}
          className="flex items-center gap-1.5 rounded-xl border-2 border-b-4 border-feather-red-dark bg-feather-red px-3 py-2 font-display text-xs font-black uppercase text-white shadow-key active:translate-y-[2px] active:border-b-0"
        >
          <LogOut className="h-4 w-4" aria-hidden="true" />
          🚪 خروج
        </button>
      </header>

      {/* Split-screen versus: my bar races the classmate's ---------------- */}
      <section className="mt-4 grid grid-cols-2 gap-3">
        {[me, rival].map((player, slot) => (
          <div
            key={player?.player_id ?? `slot-${slot}`}
            className={`rounded-2xl border-2 p-3 ${
              player?.player_id === myId
                ? "border-feather-green bg-feather-green/10"
                : "border-feather-red bg-feather-red/10"
            }`}
          >
            <div className="flex items-center gap-2">
              <span className="text-xl" aria-hidden="true">
                {player?.player_id === myId ? "🧑‍🎓" : "🤖"}
              </span>
              <span className="truncate font-display text-xs font-black text-ink" dir="auto">
                {player?.name ?? "بانتظار الخصم…"}
              </span>
              {(player?.streak ?? 0) >= 2 && (
                <span className="flex items-center gap-0.5 font-display text-[10px] font-black text-feather-orange">
                  <Flame className="h-3 w-3" aria-hidden="true" />
                  {player?.streak}
                </span>
              )}
            </div>
            <div className="mt-2 h-3 overflow-hidden rounded-full bg-surface-border">
              <motion.div
                className={`h-full rounded-full ${
                  player?.player_id === myId ? "bg-feather-green" : "bg-feather-red"
                }`}
                animate={{ width: `${progressFor(player)}%` }}
                transition={{ type: "spring", stiffness: 130, damping: 20 }}
              />
            </div>
            <p className="mt-1 font-display text-lg font-black text-ink">
              {player?.score ?? 0}
            </p>
          </div>
        ))}
      </section>

      {status === "waiting" && (
        <section className="mt-6 flex flex-1 flex-col items-center justify-center gap-3 text-center">
          <Bot className="h-10 w-10 animate-bounce text-feather-purple" aria-hidden="true" />
          <h1 className="font-display text-xl font-black text-ink">بانتظار الخصم…</h1>
          <p className="max-w-sm font-body text-sm text-ink-soft">
            شارك كود الغرفة <b dir="ltr">{roomCode}</b> مع زميلك. تبدأ المعركة تلقائياً عند
            انضمامه.
          </p>
        </section>
      )}

      {status === "playing" && question && (
        <section className="mt-6 flex flex-1 flex-col gap-4">
          <div className="flex items-center justify-between">
            <span className="rounded-xl bg-feather-purple/15 px-3 py-1 font-display text-xs font-extrabold text-feather-purple">
              {question.type}
            </span>
            <span
              className={`font-display text-lg font-black tabular-nums ${
                secondsLeft <= 10 ? "text-feather-red" : "text-ink"
              }`}
            >
              ⏱ {secondsLeft}
            </span>
          </div>

          <h2 className="font-display text-lg font-extrabold leading-snug text-ink" dir="auto">
            {question.prompt}
          </h2>

          <div className="flex flex-col gap-2">
            {question.options.map((option, optionIndex) => {
              const isAnswer = reveal === optionIndex;
              const isPicked = selected === optionIndex;
              return (
                <button
                  key={`${optionIndex}-${option}`}
                  type="button"
                  disabled={selected !== null || reveal !== null}
                  onClick={() => answer(optionIndex)}
                  className={`rounded-2xl border-2 border-b-4 px-4 py-3 text-start font-body text-sm font-bold text-ink transition-colors disabled:cursor-default ${
                    isAnswer
                      ? "border-feather-green bg-feather-green/20"
                      : isPicked
                        ? "border-feather-red bg-feather-red/20"
                        : "border-surface-border bg-surface hover:border-feather-purple/60"
                  }`}
                  dir="auto"
                >
                  {option}
                </button>
              );
            })}
          </div>

          {combo >= 2 && (
            <motion.p
              key={combo}
              initial={{ scale: 0.7, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              className="font-display text-sm font-black text-feather-orange"
            >
              🔥 سلسلة ×{combo}
            </motion.p>
          )}
        </section>
      )}

      {status === "finished" && (
        <section className="mt-6 flex flex-1 flex-col items-center justify-center gap-4 text-center">
          <Trophy
            className={`h-14 w-14 ${won ? "text-feather-yellow" : "text-ink-faint"}`}
            aria-hidden="true"
          />
          <h1 className="font-display text-2xl font-black text-ink">
            {won ? "🏆 فوز ساحق!" : "انتهت المعركة"}
          </h1>
          <ol className="w-full max-w-sm space-y-2">
            {standings.map((player, rank) => (
              <li
                key={player.player_id}
                className={`flex items-center justify-between rounded-2xl border-2 px-4 py-2.5 font-display text-sm font-extrabold ${
                  rank === 0
                    ? "border-feather-yellow bg-feather-yellow/15 text-ink"
                    : "border-surface-border bg-surface text-ink-soft"
                }`}
              >
                <span className="flex items-center gap-2">
                  {rank === 0 ? (
                    <Crown className="h-4 w-4 text-feather-yellow" aria-hidden="true" />
                  ) : (
                    <span className="w-4 text-center">{rank + 1}</span>
                  )}
                  <span dir="auto">{player.name}</span>
                </span>
                <span>{player.score}</span>
              </li>
            ))}
          </ol>

          {reward && (
            <p className="flex items-center gap-2 rounded-2xl border-2 border-feather-purple bg-feather-purple/10 px-4 py-2 font-display text-sm font-black text-feather-purple">
              <Zap className="h-4 w-4" aria-hidden="true" />+{reward.xp} XP
              <span className="text-feather-blue">💎 +{reward.gems}</span>
            </p>
          )}

          <div className="flex flex-wrap justify-center gap-2">
            <button
              type="button"
              onClick={() => router.push("/")}
              className="rounded-2xl border-2 border-b-4 border-feather-green-dark bg-feather-green px-5 py-2.5 font-display text-sm font-black uppercase text-white shadow-key active:translate-y-[2px] active:border-b-0"
            >
              إنهاء
            </button>
          </div>
        </section>
      )}

      {/* Leave confirmation: ideally exits the duel and returns home ------ */}
      {leavePrompt && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 px-4"
          role="dialog"
          aria-modal="true"
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.92, y: 16 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            className="w-full max-w-sm rounded-3xl border-2 border-surface-border bg-surface p-6 shadow-card"
          >
            <h2 className="font-display text-lg font-black text-ink">هل أنت متأكد من مغادرة التحدي؟</h2>
            <p className="mt-1 font-body text-sm text-ink-soft">
              سيتم إغلاق غرفة المعركة والعودة إلى لوحة التحكم.
            </p>
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                onClick={() => setLeavePrompt(false)}
                className="flex-1 rounded-2xl border-2 border-b-4 border-surface-border bg-surface px-4 py-2.5 font-display text-sm font-extrabold uppercase text-ink active:translate-y-[2px] active:border-b-0"
              >
                أكمل المعركة
              </button>
              <button
                type="button"
                onClick={leaveArena}
                className="flex-1 rounded-2xl border-2 border-b-4 border-feather-red-dark bg-feather-red px-4 py-2.5 font-display text-sm font-extrabold uppercase text-white active:translate-y-[2px] active:border-b-0"
              >
                خروج
              </button>
            </div>
          </motion.div>
        </div>
      )}
    </main>
  );
}
