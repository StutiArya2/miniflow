"use strict";

/* A deliberately small Markdown formatter. Build DOM nodes, never interpret HTML.
 * Handles the structures Claude commonly emits; unsupported syntax stays as text. */
function claudeInline(parent, source, depth = 0) {
  if (depth > 4) { parent.append(document.createTextNode(source)); return; }
  const pattern = /(`[^`\n]+`|\*\*[^\n]+?\*\*|__[^\n]+?__|~~[^\n]+?~~|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^\s]+?\))/g;
  let start = 0;
  for (const match of source.matchAll(pattern)) {
    parent.append(document.createTextNode(source.slice(start, match.index)));
    const token = match[0];
    let node;
    if (token.startsWith('`')) {
      node = document.createElement('code'); node.textContent = token.slice(1, -1);
    } else if (token.startsWith('[')) {
      const split = token.indexOf(']('), url = token.slice(split + 2, -1);
      let safe = false;
      try { safe = ['http:', 'https:'].includes(new URL(url).protocol); } catch { /* literal text */ }
      if (safe) {
        node = document.createElement('a'); node.href = url;
        node.target = '_blank'; node.rel = 'noopener noreferrer';
        claudeInline(node, token.slice(1, split), depth + 1);
      } else { node = document.createTextNode(token); }
    } else if (token.startsWith('_') && (
      /[A-Za-z0-9]/.test(source[match.index - 1] || '')
      || /[A-Za-z0-9]/.test(source[match.index + token.length] || '')
    )) {
      node = document.createTextNode(token);
    } else {
      const doubled = token.startsWith('**') || token.startsWith('__') || token.startsWith('~~');
      node = document.createElement(token.startsWith('~~') ? 'del' : doubled ? 'strong' : 'em');
      claudeInline(node, token.slice(doubled ? 2 : 1, doubled ? -2 : -1), depth + 1);
    }
    parent.append(node);
    start = match.index + token.length;
  }
  parent.append(document.createTextNode(source.slice(start)));
}

function claudeTableCells(line) {
  const text = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells = []; let value = '', code = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '\\' && text[i + 1] === '|') { value += '|'; i++; }
    else if (char === '`') { code = !code; value += char; }
    else if (char === '|' && !code) { cells.push(value.trim()); value = ''; }
    else { value += char; }
  }
  cells.push(value.trim());
  return cells;
}

function claudeMarkdown(source, depth = 0) {
  const root = document.createElement('div'); root.className = 'claude-prose';
  if (depth > 4) { root.textContent = source; return root; }
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const fence = line => /^\s*(`{3,}|~{3,})(.*)$/.exec(line);
  const list = line => /^(\s*)([-+*]|\d+[.)])\s+(.+)$/.exec(line);
  const heading = line => /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
  const rule = line => /^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:_\s*){3,}$/.test(line);
  const table = i => i + 1 < lines.length && lines[i].includes('|')
    && claudeTableCells(lines[i + 1]).every(cell => /^:?-{3,}:?$/.test(cell))
    && claudeTableCells(lines[i]).length === claudeTableCells(lines[i + 1]).length;
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].trim()) { i++; continue; }
    const codeFence = fence(lines[i]);
    if (codeFence) {
      const marker = codeFence[1], codeLines = []; i++;
      while (i < lines.length) {
        const closing = fence(lines[i]);
        if (closing && closing[1][0] === marker[0] && closing[1].length >= marker.length && !closing[2].trim()) { i++; break; }
        codeLines.push(lines[i++]);
      }
      const pre = document.createElement('pre'), code = document.createElement('code');
      code.textContent = codeLines.join('\n'); pre.append(code); root.append(pre); continue;
    }
    const title = heading(lines[i]);
    if (title) {
      const node = document.createElement(`h${Math.min(6, Math.max(3, title[1].length + 1))}`);
      claudeInline(node, title[2]); root.append(node); i++; continue;
    }
    if (rule(lines[i])) { root.append(document.createElement('hr')); i++; continue; }
    if (table(i)) {
      const wrap = document.createElement('div'); wrap.className = 'claude-table-scroll';
      wrap.tabIndex = 0; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', 'Response table');
      const grid = document.createElement('table'), head = document.createElement('thead'), body = document.createElement('tbody');
      const headers = claudeTableCells(lines[i]), separators = claudeTableCells(lines[i + 1]);
      const row = (cells, header) => {
        const tr = document.createElement('tr');
        headers.forEach((_, index) => {
          const cell = document.createElement(header ? 'th' : 'td');
          if (header) cell.setAttribute('scope', 'col');
          if (separators[index].endsWith(':')) cell.className = separators[index].startsWith(':') ? 'align-center' : 'align-right';
          claudeInline(cell, cells[index] || ''); tr.append(cell);
        });
        return tr;
      };
      head.append(row(headers, true)); i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) body.append(row(claudeTableCells(lines[i++]), false));
      grid.append(head, body); wrap.append(grid); root.append(wrap); continue;
    }
    if (/^\s{0,3}>/.test(lines[i])) {
      const quote = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) quote.push(lines[i++].replace(/^\s{0,3}>\s?/, ''));
      const node = document.createElement('blockquote'); node.append(claudeMarkdown(quote.join('\n'), depth + 1)); root.append(node); continue;
    }
    if (list(lines[i])) {
      const stack = []; let lastItem = null;
      while (i < lines.length) {
        const part = list(lines[i]);
        if (!part) break;
        const indent = part[1].replace(/\t/g, '    ').length, kind = /^\d/.test(part[2]) ? 'ol' : 'ul';
        while (stack.length && indent < stack[stack.length - 1].indent) stack.pop();
        let current = stack[stack.length - 1];
        if (!current || indent > current.indent || kind !== current.kind) {
          const node = document.createElement(kind);
          if (kind === 'ol') node.setAttribute('start', parseInt(part[2], 10));
          if (current && indent > current.indent && lastItem) lastItem.append(node); else root.append(node);
          current = { node, indent, kind }; stack.push(current);
        }
        lastItem = document.createElement('li'); claudeInline(lastItem, part[3]); current.node.append(lastItem); i++;
      }
      continue;
    }
    const paragraph = [lines[i++]];
    while (i < lines.length && lines[i].trim() && !fence(lines[i]) && !heading(lines[i]) && !rule(lines[i]) && !list(lines[i]) && !/^\s{0,3}>/.test(lines[i]) && !table(i)) paragraph.push(lines[i++]);
    const node = document.createElement('p');
    paragraph.join('\n').split(/ {2,}\n/).forEach((text, index) => {
      if (index) node.append(document.createElement('br'));
      claudeInline(node, text.replace(/\n/g, ' '));
    });
    root.append(node);
  }
  return root;
}
