"use client";

/**
 * RichContent — renders challenge text that contains LaTeX or fenced code.
 *
 * Zero new npm dependencies by design:
 *
 * - **Math** uses KaTeX loaded from a CDN at runtime (`window.katex`). Until the
 *   script arrives (or if the network is offline) the raw `$...$` source is
 *   shown, so the quiz never blocks on a third-party asset.
 * - **Code** uses a tiny built-in tokenizer for python / cpp / sql / js. It is
 *   deliberately small (keywords + strings + comments + numbers) — enough to
 *   make a Spot-the-Mistake snippet readable without shipping Prism.
 *
 * Everything except KaTeX's own output is rendered as React text nodes, so a
 * malicious slide can never inject markup through this component.
 */

import { Fragment, useEffect, useMemo, useState } from "react";

// Framed in a cache forever; jsDelivr serves it with long-lived immutable headers.
const KATEX_VERSION = "0.16.11";
const KATEX_CSS = `https://cdn.jsdelivr.net/npm/katex@${KATEX_VERSION}/dist/katex.min.css`;
const KATEX_JS = `https://cdn.jsdelivr.net/npm/katex@${KATEX_VERSION}/dist/katex.min.js`;

declare global {
  interface Window {
    katex?: {
      renderToString: (
        tex: string,
        options?: Record<string, unknown>,
      ) => string;
    };
  }
}

/** Injects the KaTeX stylesheet once per document. */
function useKatexCss(): void {
  useEffect(() => {
    if (document.querySelector('link[data-cl-katex="1"]')) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = KATEX_CSS;
    link.dataset.clKatex = "1";
    document.head.appendChild(link);
  }, []);
}

/** Loads katex.min.js once and flips `ready` when `window.katex` exists. */
function useKatexScript(): boolean {
  const [ready, setReady] = useState(
    () => typeof window !== "undefined" && Boolean(window.katex),
  );
  useEffect(() => {
    if (ready) return;
    let cancelled = false;
    const markReady = () => {
      if (!cancelled && window.katex) setReady(true);
    };
    const existing = document.querySelector<HTMLScriptElement>('script[data-cl-katex="1"]');
    if (existing) {
      existing.addEventListener("load", markReady);
      markReady();
      return () => {
        cancelled = true;
        existing.removeEventListener("load", markReady);
      };
    }
    const script = document.createElement("script");
    script.src = KATEX_JS;
    script.async = true;
    script.dataset.clKatex = "1";
    script.addEventListener("load", markReady);
    document.head.appendChild(script);
    return () => {
      cancelled = true;
      script.removeEventListener("load", markReady);
    };
  }, [ready]);
  return ready;
}

function KatexSpan({ tex, display }: { tex: string; display: boolean }): JSX.Element {
  const ready = useKatexScript();
  const html = useMemo(() => {
    if (!ready || typeof window === "undefined" || !window.katex) return null;
    try {
      return window.katex.renderToString(tex, {
        displayMode: display,
        throwOnError: false,
        strict: false,
        trust: false,
        output: "html",
      });
    } catch {
      return null;
    }
  }, [ready, tex, display]);

  if (html === null) {
    // Pre-load (or KaTeX failure) fallback: show the raw TeX so the question
    // is still answerable. Delimiters are kept for context.
    return (
      <code className="rounded bg-surface-muted px-1 font-mono text-[0.9em]" dir="ltr">
        {display ? `$$${tex}$$` : `$${tex}$`}
      </code>
    );
  }
  return (
    <span
      className={display ? "my-2 block overflow-x-auto text-center" : "inline-block align-middle"}
      dir="ltr"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

// --- section parsing --------------------------------------------------------

type Section =
  | { kind: "text"; value: string }
  | { kind: "code"; lang: string; value: string }
  | { kind: "math"; value: string; display: boolean };

/**
 * Split a string into text / fenced-code / LaTeX segments.
 * Fenced code wins over math so a ```python block full of `$` stays code.
 */
export function parseRichSections(input: string): Section[] {
  if (!input) return [];
  const sections: Section[] = [];
  const fence = /```([\w+-]*)\n?([\s\S]*?)```/g;
  let cursor = 0;
  let match: RegExpExecArray | null;

  const pushProse = (value: string) => {
    if (!value) return;
    // Block math first ($$...$$), then inline ($...$).
    const blocks = /(?:\$\$([\s\S]+?)\$\$)|(?:\$([^$\n]+?)\$)/g;
    let last = 0;
    let math: RegExpExecArray | null;
    while ((math = blocks.exec(value)) !== null) {
      if (math.index > last) sections.push({ kind: "text", value: value.slice(last, math.index) });
      if (math[1] !== undefined) {
        sections.push({ kind: "math", value: math[1].trim(), display: true });
      } else {
        sections.push({ kind: "math", value: (math[2] ?? "").trim(), display: false });
      }
      last = math.index + math[0].length;
    }
    if (last < value.length) sections.push({ kind: "text", value: value.slice(last) });
  };

  while ((match = fence.exec(input)) !== null) {
    if (match.index > cursor) pushProse(input.slice(cursor, match.index));
    sections.push({
      kind: "code",
      lang: (match[1] || "").toLowerCase(),
      value: (match[2] ?? "").replace(/\n$/, ""),
    });
    cursor = match.index + match[0].length;
  }
  if (cursor < input.length) pushProse(input.slice(cursor));
  return sections;
}

// --- code highlighting ------------------------------------------------------

const KEYWORDS: Record<string, string[]> = {
  python: ["def", "class", "return", "import", "from", "if", "elif", "else", "for", "while", "in", "not", "and", "or", "None", "True", "False", "with", "as", "lambda", "yield", "try", "except", "finally", "raise", "pass", "break", "continue", "self", "async", "await", "global", "nonlocal"],
  cpp: ["int", "float", "double", "char", "void", "bool", "class", "struct", "public", "private", "protected", "return", "if", "else", "for", "while", "do", "switch", "case", "break", "continue", "const", "static", "new", "delete", "namespace", "using", "template", "typename", "auto", "nullptr", "true", "false", "include", "std", "virtual", "override"],
  c: ["int", "float", "double", "char", "void", "struct", "return", "if", "else", "for", "while", "switch", "case", "break", "continue", "const", "static", "sizeof", "typedef", "unsigned", "include"],
  sql: ["SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "ON", "GROUP", "BY", "ORDER", "HAVING", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE", "CREATE", "TABLE", "INDEX", "PRIMARY", "KEY", "FOREIGN", "REFERENCES", "AND", "OR", "NOT", "NULL", "AS", "LIMIT", "DISTINCT", "COUNT", "SUM", "AVG", "MIN", "MAX"],
  js: ["function", "const", "let", "var", "return", "if", "else", "for", "while", "class", "new", "this", "async", "await", "import", "export", "from", "default", "try", "catch", "finally", "typeof", "instanceof", "null", "undefined", "true", "false", "of", "in"],
  ts: ["function", "const", "let", "var", "return", "if", "else", "for", "while", "class", "interface", "type", "enum", "new", "this", "async", "await", "import", "export", "from", "default", "try", "catch", "finally", "typeof", "null", "undefined", "true", "false"],
};

const COMMENT_PREFIX: Record<string, string> = {
  python: "#",
  sql: "--",
  cpp: "//",
  c: "//",
  js: "//",
  ts: "//",
};

interface Token {
  text: string;
  cls: string;
}

/**
 * Single-pass tokenizer: comments, strings, numbers, then words (keyword vs
 * plain). Good enough for lecture snippets and completely predictable.
 */
export function highlightCode(code: string, lang: string): Token[] {
  const words = new Set((KEYWORDS[lang] ?? KEYWORDS.python).map((w) => w.toLowerCase()));
  const comment = COMMENT_PREFIX[lang] ?? "#";
  const pattern = new RegExp(
    [
      `(${comment.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}[^\\n]*)`, // 1 comment
      `("(?:[^"\\\\]|\\\\.)*"|'(?:[^'\\\\]|\\\\.)*')`, // 2 string
      "(\\b\\d+(?:\\.\\d+)?\\b)", // 3 number
      "([A-Za-z_][A-Za-z0-9_]*)", // 4 word
    ].join("|"),
    "g",
  );
  const tokens: Token[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(code)) !== null) {
    if (match.index > last) tokens.push({ text: code.slice(last, match.index), cls: "" });
    const [full, commentText, stringText, numberText, wordText] = match;
    if (commentText !== undefined) tokens.push({ text: commentText, cls: "text-ink-faint italic" });
    else if (stringText !== undefined) tokens.push({ text: stringText, cls: "text-feather-green" });
    else if (numberText !== undefined) tokens.push({ text: numberText, cls: "text-feather-orange" });
    else if (wordText !== undefined) {
      tokens.push({
        text: wordText,
        cls: words.has(wordText.toLowerCase()) ? "text-feather-purple font-bold" : "",
      });
    } else tokens.push({ text: full, cls: "" });
    last = match.index + full.length;
  }
  if (last < code.length) tokens.push({ text: code.slice(last), cls: "" });
  return tokens;
}

function CodeBlock({ code, lang }: { code: string; lang: string }): JSX.Element {
  const lines = code.split("\n");
  return (
    <div className="my-2 overflow-x-auto rounded-2xl border-2 border-surface-border bg-[#0f172a] p-3">
      {lang && (
        <div className="mb-2 font-display text-[10px] font-extrabold uppercase tracking-widest text-slate-400">
          {lang}
        </div>
      )}
      <pre className="font-mono text-[12.5px] leading-relaxed text-slate-100" dir="ltr">
        <code>
          {lines.map((line, lineIndex) => (
            <div key={lineIndex} className="flex gap-3">
              <span
                aria-hidden="true"
                className="w-6 shrink-0 select-none text-end text-slate-500"
              >
                {lineIndex + 1}
              </span>
              <span className="whitespace-pre">
                {highlightCode(line, lang).map((token, tokenIndex) => (
                  <span key={tokenIndex} className={token.cls}>
                    {token.text}
                  </span>
                ))}
              </span>
            </div>
          ))}
        </code>
      </pre>
    </div>
  );
}

/**
 * Renders a string with inline math (`$...$`), block math (`$$...$$`) and
 * fenced code blocks. Plain text is preserved verbatim.
 */
export default function RichContent({
  text,
  className,
  inline = false,
}: {
  text: string | null | undefined;
  className?: string;
  /** Inline mode renders a <span> so it can sit inside a heading/button. */
  inline?: boolean;
}): JSX.Element {
  useKatexCss();
  const sections = useMemo(() => parseRichSections(text ?? ""), [text]);
  const Wrapper = inline ? "span" : "div";

  if (sections.length === 0) {
    return <Wrapper className={className}>{text}</Wrapper>;
  }
  return (
    <Wrapper className={className}>
      {sections.map((section, index) => (
        <Fragment key={index}>
          {section.kind === "text" && <span className="whitespace-pre-wrap">{section.value}</span>}
          {section.kind === "math" && <KatexSpan tex={section.value} display={section.display} />}
          {section.kind === "code" && <CodeBlock code={section.value} lang={section.lang} />}
        </Fragment>
      ))}
    </Wrapper>
  );
}
