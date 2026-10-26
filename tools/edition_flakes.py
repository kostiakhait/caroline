#!/usr/bin/env python3
"""Undefined-name check that sees through `from app.chat_session_shared import *`.

pyflakes cannot report undefined names in a module that uses a star import, and the chat-session mixins all do.
Here the star import is replaced, in a scratch copy, by an explicit import of every name the shared module
binds at its top level, so pyflakes sees what the module can really use.

    edition_flakes.py <backend-py dir> <pyflakes dir>   -> prints "file: message" (no line numbers), sorted
"""
import ast, io, os, re, shutil, sys, tempfile

backend, pf = sys.argv[1], sys.argv[2]
sys.path.insert(0, pf)
from pyflakes import api, reporter  # noqa: E402

app = os.path.join(backend, "app")
STAR = re.compile(r"^from (app\.[\w.]+) import \*.*$", re.M)


def top_level_names(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in tree.body:
        for sub in ast.walk(node) if isinstance(node, (ast.If, ast.Try)) else [node]:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(sub.name)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                for a in sub.names:
                    if a.name != "*":
                        names.add((a.asname or a.name).split(".")[0])
            elif isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
    return names


def module_path(mod):
    return os.path.join(backend, *mod.split(".")) + ".py"


tmp = tempfile.mkdtemp()
cache = {}
out = set()
for base, _d, files in os.walk(app):
    if "__pycache__" in base:
        continue
    for fn in files:
        if not fn.endswith(".py"):
            continue
        path = os.path.join(base, fn)
        src = open(path, encoding="utf-8").read()

        def explicit(m):
            mod = m.group(1)
            if mod not in cache:
                cache[mod] = sorted(top_level_names(module_path(mod)))
            return "from %s import %s" % (mod, ", ".join(cache[mod])) if cache[mod] else "pass"

        src2 = STAR.sub(explicit, src)
        copy = os.path.join(tmp, os.path.relpath(path, backend).replace(os.sep, "__"))
        open(copy, "w", encoding="utf-8").write(src2)
        err, warn = io.StringIO(), io.StringIO()
        api.check(src2, os.path.relpath(path, backend), reporter.Reporter(warn, err))
        for line in (warn.getvalue() + err.getvalue()).splitlines():
            if "undefined name" in line:
                out.add(re.sub(r":\d+:\d+:?", ":", line))
shutil.rmtree(tmp, ignore_errors=True)


def self_attribute_check():
    """ChatSession is one class spread over mixin files: a `self.name` that nothing in those files defines (as a
    method, a class attribute or an assignment) is a leftover of cut code."""
    files = [os.path.join(app, "chat_session.py")]
    mix = os.path.join(app, "chat_session_mixins")
    if os.path.isdir(mix):
        files += [os.path.join(mix, f) for f in sorted(os.listdir(mix)) if f.endswith(".py")]
    trees = {f: ast.parse(open(f, encoding="utf-8").read()) for f in files if os.path.exists(f)}
    known = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                known.add(node.name)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self" \
                    and isinstance(node.ctx, (ast.Store, ast.Del)):
                known.add(node.attr)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        known.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                known.add(node.target.id)
        for node in ast.walk(tree):  # setattr / getattr style definitions are rare here; slots too
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                pass
    found = set()
    for f, tree in trees.items():
        rel = os.path.relpath(f, backend)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self" \
                    and isinstance(node.ctx, ast.Load) and node.attr not in known and not node.attr.startswith("__"):
                found.add(f"{rel}: undefined name 'self.{node.attr}'")
    return found


out |= self_attribute_check()
print("\n".join(sorted(out)))
