"""Recount repository-size.json excluding tests/evaluation/docs and comments.

Run with the repository .venv Python (Pygments 2.21.0).
Python counts reuse AST/tokenize results; JavaScript uses Node's bundled Acorn
tokenizer (Node 24.19.0, Acorn 8.17.0); other languages use Pygments tokens.
The result is physical lines containing non-comment code, not statement counts.
"""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import subprocess

import pygments
from pygments import lex
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.token import Comment, Token


ROOT = Path(__file__).resolve().parent


def excluded(path):
    p = Path(path)
    if path.startswith('docs/'):
        return 'documentation_and_evidence'
    if path.startswith('eval/'):
        return 'evaluation_and_reports'
    if ('tests' in p.parts or 'test' in p.parts or p.name.startswith('test_')
            or p.name == 'conftest.py' or '.test.' in p.name):
        return 'tests_and_fixtures'
    if path.endswith('.d.ts'):
        return 'generated_declarations'
    if path.startswith('tools/') and (path.startswith(('tools/production_acceptance/', 'tools/selfcheck/'))
            or p.name.startswith(('benchmark_', 'verify_'))
            or p.name in {'self_check.py', 'qq_offline_acceptance.py', 'Start-QQAcceptance.ps1'}):
        return 'acceptance_validation_and_benchmarks'
    return None


def main():
    inventory = json.loads((ROOT / 'repository-size.json').read_text())
    process = subprocess.Popen(['git', 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    rows = []
    groups = defaultdict(lambda: {'files': 0, 'code_lines': 0})
    exclusions = defaultdict(lambda: {'files': 0, 'physical_lines': 0})
    for row in inventory['files']:
        if not row['source']:
            continue
        reason = excluded(row['path'])
        if reason:
            exclusions[reason]['files'] += 1
            exclusions[reason]['physical_lines'] += row['lines']
            continue
        result = {'path': row['path'], 'sha': row['sha'], 'category': row['category']}
        if row['extension'] == '.py':
            result['code_lines'] = row['python_token_code']
            result['method'] = 'Python AST + tokenize'
        else:
            process.stdin.write(row['sha'].encode() + b'\n')
            process.stdin.flush()
            size = int(process.stdout.readline().split()[2])
            raw = process.stdout.read(size)
            assert process.stdout.read(1) == b'\n'
            assert hashlib.sha1(b'blob ' + str(size).encode() + b'\0' + raw).hexdigest() == row['sha']
            text = raw.decode('utf-8-sig')
            if row['extension'] in {'.js', '.mjs', '.cjs'}:
                script = r"""
const fs = require('node:fs');
const acorn = require('internal/deps/acorn/acorn/dist/acorn');
const text = fs.readFileSync(0, 'utf8'), lines = text.split(/\r?\n/), code = new Set();
for (const token of acorn.tokenizer(text, {ecmaVersion:'latest', sourceType:'module', locations:true})) {
  for (let i=token.loc.start.line; i<=token.loc.end.line; i++) {
    if (lines[i-1]?.trim()) code.add(i);
  }
}
console.log(code.size);
"""
                result.update(code_lines=int(subprocess.check_output(
                    ['node', '--expose-internals', '-e', script], input=text, text=True)),
                    method='Acorn 8.17.0 tokenizer')
                rows.append(result)
                groups[row['category']]['files'] += 1
                groups[row['category']]['code_lines'] += result['code_lines']
                continue
            lexer = (get_lexer_by_name('jinja') if row['extension'] == '.jinja'
                     else get_lexer_for_filename(row['path']))
            line = 1
            code = set()
            error_tokens = 0
            for kind, value in lex(text, lexer):
                error_tokens += int(kind in Token.Error)
                for index, part in enumerate(value.split('\n')):
                    if part.strip() and kind not in Comment:
                        code.add(line + index)
                line += value.count('\n')
            result.update(code_lines=len(code), method=type(lexer).__name__, lexer_error_tokens=error_tokens)
        rows.append(result)
        groups[row['category']]['files'] += 1
        groups[row['category']]['code_lines'] += result['code_lines']
    process.stdin.close()
    process.wait()
    output = {'revision': inventory['revision'], 'pygments_version': pygments.__version__,
              'method': __doc__, 'total_files': len(rows),
              'total_code_lines': sum(row['code_lines'] for row in rows),
              'categories': dict(groups), 'exclusions': dict(exclusions), 'files': rows}
    (ROOT / 'implementation-size.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in output.items() if k not in {'files', 'method'}}, ensure_ascii=False, indent=2))
    print('Lexer errors:', [(r['path'], r['lexer_error_tokens']) for r in rows if r.get('lexer_error_tokens')])


if __name__ == '__main__':
    main()
