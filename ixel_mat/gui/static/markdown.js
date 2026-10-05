// Markdown → DOM, for model answers (Ask) and the answers runs leave (Board). Code fences, headings,
// nested lists, quotes, tables, rules, paragraphs, `code`, **bold**, *italic*, ~~strike~~. Builds
// nodes; never parses HTML. Links stay plain text: nothing a model writes becomes clickable.

import { el, copyButton } from "./common.js";

const FENCE = /^\s*(`{3,}|~{3,})\s*([\w+#.-]*)/;
const HEADING = /^#{1,6}\s/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const LIST_ITEM = /^(\s*)([-*•+]|\d{1,9}[.)])\s+(.*)$/;
const QUOTE = /^>\s?/;
const TABLE_ROW = /^\|.*\|\s*$/;
const MAX_DEPTH = 8;  // quotes in quotes, lists in lists: deeper than this shows as text
const isBlockStart = (line) => FENCE.test(line) || HEADING.test(line) || RULE.test(line) ||
  LIST_ITEM.test(line) || QUOTE.test(line) || TABLE_ROW.test(line);

export function renderMarkdown(source, depth = 0) {
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const root = document.createDocumentFragment();
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(FENCE);
    if (fence) {
      const code = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith(fence[1])) code.push(lines[i++]);
      i++;
      root.append(codeBlock(code.join("\n"), fence[2]));
    } else if (HEADING.test(line)) {
      const level = Math.min(line.match(/^#+/)[0].length + 2, 6);
      root.append(el(`h${level}`, {}, inline(line.replace(/^#+\s*/, ""))));
      i++;
    } else if (RULE.test(line)) {
      root.append(el("hr"));
      i++;
    } else if (LIST_ITEM.test(line)) {
      const [list, next] = parseList(lines, i, depth);
      root.append(list);
      i = next;
    } else if (QUOTE.test(line)) {
      const quote = [];
      while (i < lines.length && QUOTE.test(lines[i])) quote.push(lines[i++].replace(QUOTE, ""));
      root.append(el("blockquote", {}, depth < MAX_DEPTH ? renderMarkdown(quote.join("\n"), depth + 1)
        : el("p", { class: "md-plain" }, quote.join("\n"))));
    } else if (TABLE_ROW.test(line)) {
      const rows = [];
      while (i < lines.length && TABLE_ROW.test(lines[i])) rows.push(lines[i++]);
      root.append(table(rows));
    } else if (!line.trim()) {
      i++;
    } else {
      const para = el("p");
      let first = true;
      while (i < lines.length && lines[i].trim() && (first || !isBlockStart(lines[i]))) {
        if (!first) para.append(el("br"));
        para.append(...inline(lines[i++]));
        first = false;
      }
      root.append(para);
    }
  }
  return root;
}

function parseList(lines, start, depth) {
  const first = lines[start].match(LIST_ITEM);
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const list = el(ordered ? "ol" : "ul");
  if (ordered && parseInt(first[2], 10) !== 1) list.setAttribute("start", String(parseInt(first[2], 10)));
  let i = start;
  let item = null;
  while (i < lines.length) {
    const line = lines[i];
    const match = line.match(LIST_ITEM);
    if (match && match[1].length <= indent + 1 && match[1].length >= indent - 1) {
      if (/\d/.test(match[2]) !== ordered) break;
      item = el("li", {}, inline(match[3]));
      list.append(item);
      i++;
    } else if (match && match[1].length > indent && item && depth < MAX_DEPTH) {
      const [sub, next] = parseList(lines, i, depth + 1);
      item.append(sub);
      i = next;
    } else if (match && match[1].length > indent && item) {
      item.append(" ", ...inline(line.trim()));  // too deep to nest further
      i++;
    } else if (!line.trim()) {
      // A blank line ends the list unless the next item carries on at this depth
      let j = i;
      while (j < lines.length && !lines[j].trim()) j++;
      const after = j < lines.length && lines[j].match(LIST_ITEM);
      if (!after || after[1].length < indent - 1 || /\d/.test(after[2]) !== ordered) break;
      i = j;
    } else if (item && /^\s+\S/.test(line) && !isBlockStart(line.trim())) {
      item.append(" ", ...inline(line.trim()));  // a wrapped item
      i++;
    } else {
      break;
    }
  }
  return [list, i];
}

function table(rows) {
  const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const hasHeader = rows.length > 1 && /^\|[\s:|-]+\|\s*$/.test(rows[1]);
  const body = hasHeader ? rows.slice(2) : rows;
  return el("div", { class: "table-wrap" }, el("table", {},
    hasHeader ? el("thead", {}, el("tr", {}, cells(rows[0]).map((c) => el("th", {}, inline(c))))) : null,
    el("tbody", {}, body.map((row) => el("tr", {}, cells(row).map((c) => el("td", {}, inline(c))))))));
}

export function inline(text) {
  const parts = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|~~[^~]+~~|\*[^*\s][^*]*\*)/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("`")) parts.push(el("code", {}, token.slice(1, -1)));
    else if (token.startsWith("**")) parts.push(el("strong", {}, token.slice(2, -2)));
    else if (token.startsWith("~~")) parts.push(el("del", {}, token.slice(2, -2)));
    else parts.push(el("em", {}, token.slice(1, -1)));
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function codeBlock(code, lang) {
  return el("div", { class: "code" },
    el("div", { class: "code-head" }, el("span", { class: "lang" }, lang || "text"),
      copyButton(() => code, "Copy code")),
    el("pre", {}, el("code", {}, code)));
}
