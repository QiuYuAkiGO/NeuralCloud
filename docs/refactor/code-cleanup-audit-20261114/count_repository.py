"""Count a frozen Git tree, not the working directory or installed dependencies.

Physical lines include comments; nonblank lines are NOT semantic SLOC.
Python token-code lines exclude comments and standalone docstrings, but include SQL
inside assigned string literals. Other languages deliberately have no SLOC estimate.
Usage: python this_file.py --revision REV --output output.json
"""
import argparse
import ast
from collections import defaultdict
import io
import json
from pathlib import PurePosixPath
import subprocess
import tokenize

SOURCE = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.ps1', '.psm1', '.sh', '.cmd', '.bat', '.sql', '.html', '.css', '.jinja'}


def category(path):
    if path.startswith('docs/'):
        return 'documentation_embedded_source'
    if path.startswith('src/neuralcloud/'):
        return 'resident_python'
    if path.startswith('plugins/dsh-neuralcloud/src/'):
        return 'memory_plugin'
    if path.startswith('plugins/dsh-neuralcloud/test/'):
        return 'memory_plugin_tests'
    if path.startswith('plugins/mobile-chat/'):
        return 'mobile_chat_plugin'
    if path.startswith('tests/resident/'):
        return 'resident_tests'
    if path in {'tools/export_resident_contracts.py', 'tools/benchmark_resident.py'}:
        return 'resident_tools'
    if path.startswith('tools/deploy/misty_oral_v2/'):
        return 'oral_deployment'
    if path.startswith('tools/production_acceptance/misty_oral/'):
        return 'oral_acceptance'
    if path.startswith('tools/qqbot/'):
        return 'qq_bridge_tools'
    if path.startswith('tests/') or path == 'conftest.py':
        return 'legacy_tests'
    if path.startswith('eval/'):
        return 'evaluation'
    if path.startswith('tools/'):
        return 'other_tools'
    if path.startswith(('harness/', 'gui/', 'infra/')):
        return 'legacy_' + path.split('/')[0]
    return 'other_entrypoints'


def python_metrics(text):
    tree = ast.parse(text)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                docs.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
    code = set()
    excluded = {tokenize.ENCODING, tokenize.ENDMARKER, tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type not in excluded:
            code.update(range(token.start[0], token.end[0] + 1))
    code -= docs
    lines = text.splitlines()
    code = {line for line in code if lines[line - 1].strip()}
    funcs = [{'name': n.name, 'line': n.lineno, 'span': n.end_lineno - n.lineno + 1} for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return len(code), sorted(funcs, key=lambda x: -x['span'])[:3]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    revision = subprocess.check_output(['git', 'rev-parse', args.revision], text=True).strip()
    entries = subprocess.check_output(['git', 'ls-tree', '-rz', revision]).split(b'\0')
    process = subprocess.Popen(['git', 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    files = []
    for entry in entries:
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        if kind != b'blob':
            continue
        path = raw_path.decode()
        process.stdin.write(oid + b'\n')
        process.stdin.flush()
        size = int(process.stdout.readline().split()[2])
        raw = process.stdout.read(size)
        assert process.stdout.read(1) == b'\n'
        suffix = PurePosixPath(path).suffix.lower()
        try:
            text = raw.decode('utf-8-sig') if b'\0' not in raw else None
        except UnicodeDecodeError:
            text = None
        row = {'path': path, 'sha': oid.decode(), 'bytes': size, 'extension': suffix, 'text': text is not None, 'source': suffix in SOURCE and mode != b'120000'}
        if text is not None:
            row.update(lines=len(text.splitlines()), nonblank=sum(bool(x.strip()) for x in text.splitlines()))
        if row['source']:
            row['category'] = category(path)
            if suffix == '.py' and text is not None:
                try:
                    row['python_token_code'], row['longest_functions'] = python_metrics(text)
                except (SyntaxError, tokenize.TokenError) as error:
                    row['parse_error'] = type(error).__name__
        files.append(row)
    process.stdin.close()
    process.wait()
    groups = defaultdict(lambda: {'files': 0, 'lines': 0, 'nonblank': 0, 'python_token_code': 0})
    languages = defaultdict(lambda: {'files': 0, 'lines': 0, 'nonblank': 0})
    top = defaultdict(lambda: {'files': 0, 'lines': 0, 'source_lines': 0})
    for row in files:
        key = row['path'].split('/')[0] if '/' in row['path'] else '(root)'
        top[key]['files'] += 1
        top[key]['lines'] += row.get('lines', 0)
        if row['source']:
            top[key]['source_lines'] += row.get('lines', 0)
            for target in (groups[row['category']], languages[row['extension']]):
                target['files'] += 1
                target['lines'] += row.get('lines', 0)
                target['nonblank'] += row.get('nonblank', 0)
            groups[row['category']]['python_token_code'] += row.get('python_token_code', 0)
    result = {'revision': revision, 'method': __doc__, 'totals': {'tracked_files': len(files), 'text_files': sum(x['text'] for x in files), 'text_lines': sum(x.get('lines', 0) for x in files), 'source_files': sum(x['source'] for x in files), 'source_lines': sum(x.get('lines', 0) for x in files if x['source']), 'source_nonblank': sum(x.get('nonblank', 0) for x in files if x['source'])}, 'categories': dict(sorted(groups.items())), 'languages': dict(sorted(languages.items())), 'top_level': dict(sorted(top.items())), 'files': files}
    with open(args.output, 'w') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps({k: v for k, v in result.items() if k not in {'files', 'method'}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
