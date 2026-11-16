import type { HLJSApi } from "highlight.js";

/**
 * Lazy syntax highlighting.
 *
 * Only the core and the language modules a user actually opens are
 * loaded, so the first page view carries no grammar.  Output is HTML from
 * highlight.js (which escapes the source text); callers may inject it,
 * but must never pass file content through `dangerouslySetInnerHTML`
 * without this function.
 */

export const MAX_HIGHLIGHT_BYTES = 256 * 1024;
export const MAX_HIGHLIGHT_LINES = 2000;

type LanguageModule = { default: unknown };

const loaders: Record<string, () => Promise<LanguageModule>> = {
  python: () => import("highlight.js/lib/languages/python"),
  typescript: () => import("highlight.js/lib/languages/typescript"),
  javascript: () => import("highlight.js/lib/languages/javascript"),
  json: () => import("highlight.js/lib/languages/json"),
  yaml: () => import("highlight.js/lib/languages/yaml"),
  ini: () => import("highlight.js/lib/languages/ini"),
  markdown: () => import("highlight.js/lib/languages/markdown"),
  bash: () => import("highlight.js/lib/languages/bash"),
  css: () => import("highlight.js/lib/languages/css"),
  scss: () => import("highlight.js/lib/languages/scss"),
  less: () => import("highlight.js/lib/languages/less"),
  xml: () => import("highlight.js/lib/languages/xml"),
  sql: () => import("highlight.js/lib/languages/sql"),
  rust: () => import("highlight.js/lib/languages/rust"),
  go: () => import("highlight.js/lib/languages/go"),
  java: () => import("highlight.js/lib/languages/java"),
  diff: () => import("highlight.js/lib/languages/diff"),
  dockerfile: () => import("highlight.js/lib/languages/dockerfile"),
  makefile: () => import("highlight.js/lib/languages/makefile"),
};

let core: Promise<HLJSApi> | null = null;
const registered = new Set<string>();
const pending = new Map<string, Promise<HLJSApi | null>>();

async function getHighlighter(language: string): Promise<HLJSApi | null> {
  const load = loaders[language];
  if (!load) return null;

  const existing = pending.get(language);
  if (existing) return existing;

  const promise = (async () => {
    try {
      const [api, module] = await Promise.all([
        (core ??= import("highlight.js/lib/core").then(
          (value) => value.default as HLJSApi,
        )),
        load(),
      ]);
      if (!registered.has(language)) {
        api.registerLanguage(language, module.default as never);
        registered.add(language);
      }
      return api;
    } catch {
      // A failed chunk must not break the preview: callers fall back to
      // plain text.
      return null;
    } finally {
      pending.delete(language);
    }
  })();

  pending.set(language, promise);
  return promise;
}

const previewCache = new Map<string, string>();
const lineCache = new Map<string, string>();
const PREVIEW_CACHE_LIMIT = 20;
const LINE_CACHE_LIMIT = 2000;

function remember(
  cache: Map<string, string>,
  key: string,
  value: string,
  limit: number,
): void {
  if (cache.size >= limit) {
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) cache.delete(oldest);
  }
  cache.set(key, value);
}

function highlightWith(
  api: HLJSApi,
  code: string,
  language: string,
): string | null {
  try {
    return api.highlight(code, {
      language,
      ignoreIllegals: true,
    }).value;
  } catch {
    return null;
  }
}

/** Highlight a whole file for the preview. `cacheKey` is path + mtime. */
export async function highlightCode(
  code: string,
  language: string | null,
  cacheKey: string,
): Promise<string | null> {
  if (!language) return null;
  if (code.length > MAX_HIGHLIGHT_BYTES) return null;
  const lines = code.split("\n");
  if (lines.length > MAX_HIGHLIGHT_LINES) return null;
  // Minified files can be one enormous line; the highlighter is not a
  // formatter, so leave those plain.
  if (lines.some((line) => line.length > 4000)) return null;

  const key = `${cacheKey}\0${language}`;
  const cached = previewCache.get(key);
  if (cached !== undefined) return cached;

  const api = await getHighlighter(language);
  if (!api) return null;
  const result = highlightWith(api, code, language);
  if (result !== null) {
    remember(previewCache, key, result, PREVIEW_CACHE_LIMIT);
  }
  return result;
}

/** Highlight one diff line. Identical lines share a cache entry. */
export async function highlightLine(
  text: string,
  language: string | null,
): Promise<string | null> {
  if (!language || !text) return null;
  if (text.length > 4000) return null;
  const key = `${language}\0${text}`;
  const cached = lineCache.get(key);
  if (cached !== undefined) return cached;

  const api = await getHighlighter(language);
  if (!api) return null;
  const result = highlightWith(api, text, language);
  if (result !== null) remember(lineCache, key, result, LINE_CACHE_LIMIT);
  return result;
}
