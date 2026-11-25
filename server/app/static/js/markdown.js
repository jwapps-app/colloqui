// Message formatting, all built with DOM nodes (never innerHTML): `code`,
// **bold**, *italic*, ~~strike~~, ```blocks```, lists, quotes, task lines,
// auto-linked URLs and the @me highlight. Imports nothing that touches the
// network or the page, so it runs under Node for tests (tests/js/).
import { shortDate } from './format.js';
import { state } from './state.js';
import { isTaskLine, parseTaskLine } from './tasks.js';

function linkEl(url, label) {
  const a = document.createElement('a');
  a.href = url;
  a.textContent = label;
  a.target = '_blank';
  a.rel = 'noopener noreferrer nofollow';
  return a;
}

export function renderInline(text, container) {
  // Order matters: markdown links before bare URLs.
  const combined = new RegExp(
    '(`[^`\\n]+`)'                                 // 1 inline code
    + '|(\\[[^\\]\\n]+\\]\\(https?:\\/\\/[^\\s)]+\\))'  // 2 [text](url)
    + '|(\\*\\*[^*\\n]+\\*\\*)'                     // 3 bold
    + '|(~~[^~\\n]+~~)'                             // 4 strikethrough
    + '|(\\*[^*\\n]+\\*)'                           // 5 italic
    + '|(https?:\\/\\/[^\\s<>"]+)'                  // 6 bare url
    + (state.me ? `|(@${state.me.username}\\b)` : ''),          // 7 @me
    'gi'
  );
  let last = 0, m;
  while ((m = combined.exec(text)) !== null) {
    container.appendChild(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (m[1]) {
      const el = document.createElement('code');
      el.textContent = tok.slice(1, -1);
      container.appendChild(el);
    } else if (m[2]) {
      const mk = tok.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
      container.appendChild(linkEl(mk[2], mk[1]));
    } else if (m[3]) {
      const el = document.createElement('strong');
      el.textContent = tok.slice(2, -2);
      container.appendChild(el);
    } else if (m[4]) {
      const el = document.createElement('del');
      el.textContent = tok.slice(2, -2);
      container.appendChild(el);
    } else if (m[5]) {
      const el = document.createElement('em');
      el.textContent = tok.slice(1, -1);
      container.appendChild(el);
    } else if (m[6]) {
      let url = tok, trail = '';
      while (/[.,;:!?)\]]$/.test(url)) { trail = url.slice(-1) + trail; url = url.slice(0, -1); }
      container.appendChild(linkEl(url, url));
      if (trail) container.appendChild(document.createTextNode(trail));
    } else {
      const el = document.createElement('span');
      el.className = 'mention';
      el.textContent = tok;
      container.appendChild(el);
    }
    last = m.index + tok.length;
  }
  container.appendChild(document.createTextNode(text.slice(last)));
}

function isBlockLine(line) {
  return isTaskLine(line) || /^- /.test(line) || /^\d+\. /.test(line)
    || /^> /.test(line) || line.trim().startsWith('```');
}

// `cleared`: line index -> when that task was ticked. `onToggleTask(line,
// checked)` is called when a task's box is clicked and must return a promise;
// if it rejects, the box flips back.
export function renderBody(text, cleared, onToggleTask) {
  cleared = cleared || {};
  const body = document.createElement('div');
  body.className = 'body';
  const lines = text.split('\n');
  let inCode = false;
  let codeBuf = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith('```')) {
      if (inCode) {
        const pre = document.createElement('pre');
        pre.className = 'code-block';
        pre.textContent = codeBuf.join('\n');
        body.appendChild(pre);
        codeBuf = [];
        inCode = false;
      } else {
        inCode = true;
      }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }

    let m;
    const task = parseTaskLine(line);
    if (task) {
      const row = document.createElement('div');
      const done = task.done;
      row.className = 'task-line' + (done ? ' done' : '');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = done;
      cb.disabled = !onToggleTask;
      const lineIndex = i;
      cb.onchange = () => {
        onToggleTask(lineIndex, cb.checked).catch(() => { cb.checked = !cb.checked; });
      };
      row.appendChild(cb);
      const span = document.createElement('span');
      renderInline(task.text, span);
      row.appendChild(span);
      if (done && cleared[String(i)]) {
        const stamp = document.createElement('span');
        stamp.className = 'task-cleared';
        stamp.textContent = 'cleared ' + shortDate(cleared[String(i)]);
        row.appendChild(stamp);
      }
      body.appendChild(row);
      continue;
    }
    if ((m = line.match(/^- (.*)$/))) {
      const row = document.createElement('div');
      row.className = 'bullet-line';
      const dot = document.createElement('span');
      dot.className = 'bullet';
      dot.textContent = '•';
      row.appendChild(dot);
      const span = document.createElement('span');
      renderInline(m[1], span);
      row.appendChild(span);
      body.appendChild(row);
      continue;
    }
    if ((m = line.match(/^(\d+)\. (.*)$/))) {
      const row = document.createElement('div');
      row.className = 'bullet-line';
      const num = document.createElement('span');
      num.className = 'bullet';
      num.textContent = m[1] + '.';
      row.appendChild(num);
      const span = document.createElement('span');
      renderInline(m[2], span);
      row.appendChild(span);
      body.appendChild(row);
      continue;
    }
    if ((m = line.match(/^> (.*)$/))) {
      const row = document.createElement('div');
      row.className = 'blockquote-line';
      renderInline(m[1], row);
      body.appendChild(row);
      continue;
    }
    renderInline(line, body);
    if (i < lines.length - 1 && !isBlockLine(lines[i + 1])) {
      body.appendChild(document.createTextNode('\n'));
    }
  }
  if (inCode) {
    // unclosed fence — render what we have as text
    renderInline('```', body);
    if (codeBuf.length) body.appendChild(document.createTextNode('\n' + codeBuf.join('\n')));
  }
  return body;
}
