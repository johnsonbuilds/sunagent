/* A small Markdown renderer for assistant messages.
 *
 * It is deliberately self-contained: the web UI must work offline, so
 * no CDN and no bundler-level plugin is involved. The supported subset
 * covers what models actually emit in chat: paragraphs, headings,
 * lists, quotes, fenced code, inline code, emphasis, links, rules, and
 * tables. This is a direct port of the original dependency-free client.
 */

export function escapeHtml(text: unknown): string {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(text: string): string {
  return escapeHtml(text).replace(/'/g, "&#39;");
}

function renderInline(text: string): string {
  const codes: string[] = [];
  let out = String(text).replace(/`([^`\n]+)`/g, (_match, code: string) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });

  out = escapeHtml(out);
  out = out.replace(
    /!\[([^\]]*)\]\(([^)\s]+)\)/g,
    (_m, alt: string, src: string) =>
      `<img alt="${escapeAttr(alt)}" src="${escapeAttr(src)}">`,
  );
  out = out.replace(
    /\[([^\]]+)\]\(([^)\s]+)\)/g,
    (_m, label: string, href: string) =>
      `<a href="${escapeAttr(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`,
  );
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  out = out.replace(/\u0000(\d+)\u0000/g, (_m, index: string) =>
    `<code>${escapeHtml(codes[Number(index)])}</code>`,
  );
  return out;
}

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function isTableDivider(line: string): boolean {
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
}

export function render(text: unknown): string {
  const lines = String(text == null ? "" : text)
    .replace(/\r\n?/g, "\n")
    .split("\n");
  const out: string[] = [];
  let paragraph: string[] = [];

  function flushParagraph() {
    if (paragraph.length) {
      out.push(`<p>${renderInline(paragraph.join("\n"))}</p>`);
      paragraph = [];
    }
  }

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    const fence = /^\s*```+\s*([\w+-]*)\s*$/.exec(line);
    if (fence) {
      flushParagraph();
      const buffer: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) {
        buffer.push(lines[i]);
        i += 1;
      }
      i += 1;
      const lang = fence[1] ? ` data-lang="${escapeAttr(fence[1])}"` : "";
      out.push(
        `<pre class="code"${lang}><code>${escapeHtml(buffer.join("\n"))}</code></pre>`,
      );
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      const level = Math.min(heading[1].length, 6);
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
      flushParagraph();
      out.push("<hr>");
      i += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      const quote: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      out.push(`<blockquote>${render(quote.join("\n"))}</blockquote>`);
      continue;
    }

    if (
      i + 1 < lines.length &&
      line.indexOf("|") !== -1 &&
      isTableDivider(lines[i + 1])
    ) {
      flushParagraph();
      const headers = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].indexOf("|") !== -1 && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      let table = "<table><thead><tr>";
      headers.forEach((cell) => {
        table += `<th>${renderInline(cell)}</th>`;
      });
      table += "</tr></thead><tbody>";
      rows.forEach((row) => {
        table += "<tr>";
        headers.forEach((_cell, index) => {
          table += `<td>${renderInline(row[index] || "")}</td>`;
        });
        table += "</tr>";
      });
      table += "</tbody></table>";
      out.push(table);
      continue;
    }

    const bullet = /^\s*([-*+])\s+(.*)$/.exec(line);
    const numbered = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const ordered = Boolean(numbered);
      const pattern = ordered
        ? /^\s*(\d+)[.)]\s+(.*)$/
        : /^\s*([-*+])\s+(.*)$/;
      const items: string[] = [];
      while (i < lines.length) {
        const match = pattern.exec(lines[i]);
        if (!match) break;
        let item = match[2];
        i += 1;
        while (
          i < lines.length &&
          /^\s{2,}\S/.test(lines[i]) &&
          !pattern.test(lines[i])
        ) {
          item += ` ${lines[i].trim()}`;
          i += 1;
        }
        items.push(item);
      }
      const tag = ordered ? "ol" : "ul";
      let html = `<${tag}>`;
      items.forEach((item) => {
        html += `<li>${renderInline(item)}</li>`;
      });
      html += `</${tag}>`;
      out.push(html);
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      i += 1;
      continue;
    }

    paragraph.push(line);
    i += 1;
  }

  flushParagraph();
  return out.join("");
}
