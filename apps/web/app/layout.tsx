import type { Metadata, Viewport } from "next";
import { cookies } from "next/headers";
import "tailwindcss/tailwind.css";

export const metadata: Metadata = {
  title: {
    default: "CampusLingo — محاضراتك إلى تدريب يومي",
    template: "%s · CampusLingo",
  },
  description:
    "CampusLingo turns university lecture PDFs into short gamified challenges with FSRS-4.5 spaced repetition.",
  applicationName: "CampusLingo",
  keywords: ["spaced repetition", "FSRS", "micro-learning", "university", "gamification"],
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  themeColor: "#58CC02",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // Language/direction live in a cookie so RTL is correct on first paint.
  const lang = cookies().get("cl-lang")?.value === "en" ? "en" : "ar";
  const dir = lang === "ar" ? "rtl" : "ltr";

  return (
    <html lang={lang} dir={dir}>
      <body
        className="min-h-screen bg-surface-muted font-body text-ink antialiased"
        style={
          {
            "--cl-green": "#58CC02",
            "--cl-green-dark": "#46A302",
            "--cl-green-light": "#89E219",
            "--cl-blue": "#1CB0F6",
            "--cl-blue-dark": "#1899D6",
            "--cl-red": "#FF4B4B",
            "--cl-red-dark": "#EA2B2B",
            "--cl-yellow": "#FFC800",
            "--cl-purple": "#CE82FF",
            "--cl-orange": "#FF9600",
            "--cl-ink": "#3C3C3C",
            "--cl-ink-soft": "#777777",
            "--cl-ink-faint": "#AFAFAF",
            "--cl-surface": "#FFFFFF",
            "--cl-surface-muted": "#F7F7F7",
            "--cl-surface-border": "#E5E5E5",
            "--font-display":
              '"Nunito", "Baloo 2", "Segoe UI", system-ui, -apple-system, "Noto Kufi Arabic", sans-serif',
            "--font-body":
              '"Inter", "Segoe UI", system-ui, -apple-system, "Noto Naskh Arabic", sans-serif',
          } as React.CSSProperties
        }
      >
        <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col">{children}</div>
      </body>
    </html>
  );
}
