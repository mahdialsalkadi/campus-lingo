import type { Metadata, Viewport } from "next";
import { cookies } from "next/headers";
import AppShell from "@/components/AppShell";
import PwaRegistrar from "@/components/PwaRegistrar";
import "tailwindcss/tailwind.css";
import "./theme.css";

export const metadata: Metadata = {
  title: {
    default: "CampusLingo — محاضراتك إلى تدريب يومي",
    template: "%s · CampusLingo",
  },
  description:
    "CampusLingo turns university lecture PDFs into short gamified challenges with FSRS-4.5 spaced repetition.",
  applicationName: "CampusLingo",
  keywords: ["spaced repetition", "FSRS", "micro-learning", "university", "gamification"],
  manifest: "/manifest.json",
  icons: {
    icon: "/icon.svg",
    apple: "/icon.svg",
  },
  appleWebApp: {
    capable: true,
    title: "CampusLingo",
    statusBarStyle: "black-translucent",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#58CC02" },
    { media: "(prefers-color-scheme: dark)", color: "#121b21" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // Language/direction live in a cookie so RTL is correct on first paint.
  const lang = cookies().get("cl-lang")?.value === "en" ? "en" : "ar";
  const dir = lang === "ar" ? "rtl" : "ltr";

  return (
    <html lang={lang} dir={dir} suppressHydrationWarning>
      <body className="min-h-screen bg-surface-muted font-body text-ink antialiased">
        {/* No-flash dark mode: applies the persisted theme before first paint.
            `page` is the zustand-persist key for useQuizStore. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var s=JSON.parse(localStorage.getItem('campuslingo-progress-v2')||'{}');var t=(s.state&&s.state.theme)==='dark'?'dark':((s.state&&s.state.theme)==='light'?'light':(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'));document.documentElement.setAttribute('data-theme',t);}catch(e){}})();`,
          }}
        />
        <AppShell>{children}</AppShell>
        <PwaRegistrar />
      </body>
    </html>
  );
}
