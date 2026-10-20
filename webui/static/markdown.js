/* A small Markdown renderer for assistant messages.
 *
 * It is deliberately self-contained: the web UI must work offline, so
 * no CDN and no bundler are involved. The supported subset covers what
 * models actually emit in chat: paragraphs, headings, lists, quotes,
 * fenced code, inline code, emphasis, links, rules, and tables.
 */
(function (global) {
  "use strict";

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeAttr(text) {
    return escapeHtml(text).replace(/'/g, "&#39;");
  }

  function renderInline(text) {
    var codes = [];
    var out = String(text).replace(/`([^`\n]+)`/g, function (match, code) {
      codes.push(code);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });

    out = escapeHtml(out);
    out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, function (m, alt, src) {
      return '<img alt="' + escapeAttr(alt) + '" src="' + escapeAttr(src) + '">';
    });
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, label, href) {
      return '<a href="' + escapeAttr(href) + '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
    });
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    out = out.replace(/\u0000(\d+)\u0000/g, function (m, index) {
      return "<code>" + escapeHtml(codes[Number(index)]) + "</code>";
    });
    return out;
  }

  function splitRow(line) {
    var trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map(function (cell) { return cell.trim(); });
  }

  function isTableDivider(line) {
    return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  }

  function render(text) {
    var lines = String(text == null ? "" : text).replace(/\r\n?/g, "\n").split("\n");
    var out = [];
    var paragraph = [];

    function flushParagraph() {
      if (paragraph.length) {
        out.push("<p>" + renderInline(paragraph.join("\n")) + "</p>");
        paragraph = [];
      }
    }

    var i = 0;
    while (i < lines.length) {
      var line = lines[i];

      var fence = /^\s*```+\s*([\w+-]*)\s*$/.exec(line);
      if (fence) {
        flushParagraph();
        var buffer = [];
        i += 1;
        while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) {
          buffer.push(lines[i]);
          i += 1;
        }
        i += 1;
        var lang = fence[1] ? ' data-lang="' + escapeAttr(fence[1]) + '"' : "";
        out.push('<pre class="code"' + lang + "><code>" + escapeHtml(buffer.join("\n")) + "</code></pre>");
        continue;
      }

      var heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        flushParagraph();
        var level = Math.min(heading[1].length, 6);
        out.push("<h" + level + ">" + renderInline(heading[2]) + "</h" + level + ">");
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
        var quote = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ""));
          i += 1;
        }
        out.push("<blockquote>" + render(quote.join("\n")) + "</blockquote>");
        continue;
      }

      if (i + 1 < lines.length && line.indexOf("|") !== -1 && isTableDivider(lines[i + 1])) {
        flushParagraph();
        var headers = splitRow(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf("|") !== -1 && lines[i].trim()) {
          rows.push(splitRow(lines[i]));
          i += 1;
        }
        var table = "<table><thead><tr>";
        headers.forEach(function (cell) {
          table += "<th>" + renderInline(cell) + "</th>";
        });
        table += "</tr></thead><tbody>";
        rows.forEach(function (row) {
          table += "<tr>";
          headers.forEach(function (_, index) {
            table += "<td>" + renderInline(row[index] || "") + "</td>";
          });
          table += "</tr>";
        });
        table += "</tbody></table>";
        out.push(table);
        continue;
      }

      var bullet = /^\s*([-*+])\s+(.*)$/.exec(line);
      var numbered = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
      if (bullet || numbered) {
        flushParagraph();
        var ordered = Boolean(numbered);
        var pattern = ordered ? /^\s*(\d+)[.)]\s+(.*)$/ : /^\s*([-*+])\s+(.*)$/;
        var items = [];
        while (i < lines.length) {
          var match = pattern.exec(lines[i]);
          if (!match) break;
          var item = match[2];
          i += 1;
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !pattern.test(lines[i])) {
            item += " " + lines[i].trim();
            i += 1;
          }
          items.push(item);
        }
        var tag = ordered ? "ol" : "ul";
        var html = "<" + tag + ">";
        items.forEach(function (item) {
          html += "<li>" + renderInline(item) + "</li>";
        });
        html += "</" + tag + ">";
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

  global.SunMarkdown = { render: render, escapeHtml: escapeHtml };
})(typeof window !== "undefined" ? window : this);
