"use client";

import { useEffect } from "react";

/**
 * Registers the hand-written service worker so the app is installable on
 * iOS/Android home screens and the shell loads in airplane mode.
 *
 * Skipped in development: a caching SW makes HMR confusing ("why is my edit
 * not showing?"), and offline study is only meaningful against a built app.
 */
export default function PwaRegistrar() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;
    if (process.env.NODE_ENV !== "production") return;

    const register = () => {
      navigator.serviceWorker.register("/sw.js").catch(() => undefined);
    };

    if (document.readyState === "complete") register();
    else {
      window.addEventListener("load", register);
      return () => window.removeEventListener("load", register);
    }
  }, []);

  return null;
}
