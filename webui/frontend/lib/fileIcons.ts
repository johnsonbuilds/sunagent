export interface FileLanguage {
  id: string;
  label: string;
  tone: string;
  highlight: string | null;
}

const BY_EXTENSION: Record<string, Omit<FileLanguage, "id">> = {
  py: { label: "PY", tone: "is-py", highlight: "python" },
  pyi: { label: "PY", tone: "is-py", highlight: "python" },
  ts: { label: "TS", tone: "is-ts", highlight: "typescript" },
  tsx: { label: "TS", tone: "is-ts", highlight: "typescript" },
  js: { label: "JS", tone: "is-js", highlight: "javascript" },
  jsx: { label: "JS", tone: "is-js", highlight: "javascript" },
  mjs: { label: "JS", tone: "is-js", highlight: "javascript" },
  cjs: { label: "JS", tone: "is-js", highlight: "javascript" },
  json: { label: "{}", tone: "is-json", highlight: "json" },
  jsonc: { label: "{}", tone: "is-json", highlight: "json" },
  yaml: { label: "YML", tone: "is-yaml", highlight: "yaml" },
  yml: { label: "YML", tone: "is-yaml", highlight: "yaml" },
  toml: { label: "TML", tone: "is-toml", highlight: "ini" },
  md: { label: "MD", tone: "is-md", highlight: "markdown" },
  mdx: { label: "MD", tone: "is-md", highlight: "markdown" },
  sh: { label: "SH", tone: "is-sh", highlight: "bash" },
  bash: { label: "SH", tone: "is-sh", highlight: "bash" },
  zsh: { label: "SH", tone: "is-sh", highlight: "bash" },
  css: { label: "CSS", tone: "is-css", highlight: "css" },
  scss: { label: "CSS", tone: "is-css", highlight: "scss" },
  less: { label: "CSS", tone: "is-css", highlight: "less" },
  html: { label: "<>", tone: "is-html", highlight: "xml" },
  htm: { label: "<>", tone: "is-html", highlight: "xml" },
  xml: { label: "<>", tone: "is-html", highlight: "xml" },
  svg: { label: "<>", tone: "is-html", highlight: "xml" },
  sql: { label: "SQL", tone: "is-sql", highlight: "sql" },
  rs: { label: "RS", tone: "is-rs", highlight: "rust" },
  go: { label: "GO", tone: "is-go", highlight: "go" },
  java: { label: "JV", tone: "is-java", highlight: "java" },
  diff: { label: "±", tone: "is-diff", highlight: "diff" },
  patch: { label: "±", tone: "is-diff", highlight: "diff" },
};

const BY_NAME: Record<string, Omit<FileLanguage, "id">> = {
  dockerfile: { label: "DKR", tone: "is-sh", highlight: "dockerfile" },
  makefile: { label: "MK", tone: "is-sh", highlight: "makefile" },
  ".gitignore": { label: "GIT", tone: "is-md", highlight: null },
  ".gitattributes": { label: "GIT", tone: "is-md", highlight: null },
  ".dockerignore": { label: "DKR", tone: "is-sh", highlight: null },
  "package.json": { label: "{}", tone: "is-json", highlight: "json" },
  "tsconfig.json": { label: "{}", tone: "is-json", highlight: "json" },
  "uv.lock": { label: "LCK", tone: "is-toml", highlight: "ini" },
};

function baseName(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash === -1 ? path : path.slice(slash + 1);
}

function extensionOf(name: string): string {
  const index = name.lastIndexOf(".");
  return index <= 0 ? "" : name.slice(index + 1).toLowerCase();
}

export function languageFor(path: string): FileLanguage | null {
  const name = baseName(path).toLowerCase();
  const special = BY_NAME[name];
  if (special) return { id: name, ...special };
  const extension = extensionOf(name);
  const entry = BY_EXTENSION[extension];
  return entry ? { id: extension, ...entry } : null;
}

export function highlightLanguage(name: string): string | null {
  return languageFor(name)?.highlight ?? null;
}
