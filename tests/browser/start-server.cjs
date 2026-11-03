'use strict';
const { spawn, execFileSync } = require('node:child_process');
process.chdir(require('node:path').resolve(__dirname, '../..'));
const windowsWsl = process.platform === 'win32' && process.cwd().startsWith('\\\\wsl.localhost\\');
let command = 'uv', args = ['run', 'python', '-m', 'tests.browser.serve'];
if (windowsWsl) {
  const match = process.cwd().match(/^\\\\wsl\.localhost\\([^\\]+)\\(.*)$/);
  const root = '/' + match[2].replaceAll('\\', '/');
  command = 'wsl.exe';
  args = ['-d', match[1], '--cd', root, '--', 'bash', '-lc', 'exec uv run python -m tests.browser.serve'];
}
const child = spawn(command, args, { stdio: 'inherit', windowsHide: true });
const stop = () => {
  if (process.platform === 'win32') {
    try { execFileSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true }); } catch {}
  } else child.kill('SIGTERM');
};
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
child.on('error', error => { console.error(error.message); process.exit(1); });
child.on('exit', code => process.exit(code ?? 0));
