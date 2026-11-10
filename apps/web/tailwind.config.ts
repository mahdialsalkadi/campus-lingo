import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./stores/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      // Brand tokens are CSS variables (declared in app/layout.tsx) so the
      // palette can be re-themed per locale / per tenant without a rebuild.
      colors: {
        feather: {
          green: "var(--cl-green, #58CC02)",
          "green-dark": "var(--cl-green-dark, #46A302)",
          "green-light": "var(--cl-green-light, #89E219)",
          blue: "var(--cl-blue, #1CB0F6)",
          "blue-dark": "var(--cl-blue-dark, #1899D6)",
          red: "var(--cl-red, #FF4B4B)",
          "red-dark": "var(--cl-red-dark, #EA2B2B)",
          yellow: "var(--cl-yellow, #FFC800)",
          purple: "var(--cl-purple, #CE82FF)",
          orange: "var(--cl-orange, #FF9600)",
        },
        ink: {
          DEFAULT: "var(--cl-ink, #3C3C3C)",
          soft: "var(--cl-ink-soft, #777777)",
          faint: "var(--cl-ink-faint, #AFAFAF)",
        },
        surface: {
          DEFAULT: "var(--cl-surface, #FFFFFF)",
          muted: "var(--cl-surface-muted, #F7F7F7)",
          border: "var(--cl-surface-border, #E5E5E5)",
        },
      },
      fontFamily: {
        display: ["var(--font-display)", "system-ui", "sans-serif"],
        body: ["var(--font-body)", "system-ui", "sans-serif"],
      },
      boxShadow: {
        key: "0 4px 0 0 rgba(0,0,0,0.18)",
        "key-sm": "0 2px 0 0 rgba(0,0,0,0.16)",
        card: "0 2px 12px rgba(0,0,0,0.08)",
      },
      keyframes: {
        shake: {
          "0%, 100%": { transform: "translateX(0)" },
          "20%": { transform: "translateX(-8px)" },
          "40%": { transform: "translateX(8px)" },
          "60%": { transform: "translateX(-5px)" },
          "80%": { transform: "translateX(5px)" },
        },
        pop: {
          "0%": { transform: "scale(0.85)", opacity: "0" },
          "60%": { transform: "scale(1.06)", opacity: "1" },
          "100%": { transform: "scale(1)", opacity: "1" },
        },
        floatUp: {
          "0%": { transform: "translateY(0)", opacity: "0" },
          "25%": { transform: "translateY(-10px)", opacity: "1" },
          "100%": { transform: "translateY(-46px)", opacity: "0" },
        },
        pulseRing: {
          "0%": { boxShadow: "0 0 0 0 rgba(88,204,2,0.55)" },
          "100%": { boxShadow: "0 0 0 16px rgba(88,204,2,0)" },
        },
        slideIn: {
          "0%": { transform: "translateX(24px)", opacity: "0" },
          "100%": { transform: "translateX(0)", opacity: "1" },
        },
        bounceSlow: {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-8px)" },
        },
        wiggle: {
          "0%, 100%": { transform: "rotate(0deg)" },
          "25%": { transform: "rotate(-8deg)" },
          "75%": { transform: "rotate(8deg)" },
        },
        popFun: {
          "0%": { transform: "scale(0.3)", opacity: "0" },
          "55%": { transform: "scale(1.12)", opacity: "1" },
          "75%": { transform: "scale(0.94)" },
          "100%": { transform: "scale(1)" },
        },
        victory: {
          "0%": { transform: "scale(0.6)", opacity: "0" },
          "40%": { transform: "scale(1.15)", opacity: "1" },
          "60%": { transform: "scale(0.97)" },
          "80%": { transform: "scale(1.03)" },
          "100%": { transform: "scale(1)" },
        },
      },      animation: {
        shake: "shake 0.4s ease-in-out",
        pop: "pop 0.25s ease-out",
        "float-up": "floatUp 1s ease-out forwards",
        "pulse-ring": "pulseRing 0.9s ease-out",
        "slide-in": "slideIn 0.28s ease-out",
        "bounce-slow": "bounceSlow 1.6s ease-in-out infinite",
        wiggle: "wiggle 0.6s ease-in-out",
        "pop-fun": "popFun 0.45s cubic-bezier(0.34, 1.56, 0.64, 1) both",
        victory: "victory 0.7s cubic-bezier(0.34, 1.56, 0.64, 1) both",
      },
    },
  },
  plugins: [require("@tailwindcss/typography")],
};

export default config;
