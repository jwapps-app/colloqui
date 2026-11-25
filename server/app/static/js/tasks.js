// Task lines: the checklist grammar inside message bodies. The ONLY place it
// is spelled out on the client. (The server's copy is app/tasks.py; the two
// must agree, since the server counts open tasks and the client draws them.)
//
// A task line starts with `[ ] ` (open) or `[x] ` (done).

const TASK_LINE = /^\[( |x)\] (.*)$/i;

export const OPEN_TASK_PREFIX = '[ ] ';

// { done, text } for a task line, or null for any other line.
export function parseTaskLine(line) {
  const m = line.match(TASK_LINE);
  return m ? { done: m[1].toLowerCase() === 'x', text: m[2] } : null;
}

export function isTaskLine(line) {
  return TASK_LINE.test(line);
}

// Whether a message body contains any task line. Only such messages can change
// the open-task counts, so everything else skips the /tasks refetch.
export function hasTaskLine(content) {
  return !!content && content.split('\n').some(isTaskLine);
}
