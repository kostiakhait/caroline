#!/usr/bin/env python3
"""Conditional compilation of Caroline for the IntelliCat edition.

The IntelliCat device answers through SquirrelWisdom only: the Claude and OpenAI answer sources do not exist in
its build, with everything that serves them (engines, login, model choice, their UI). The source tree stays
one tree: the code that belongs to those sources is marked, and this script removes it from a build output.

    tools/edition_build.py <output_root>        # processes a dist-linux style tree IN PLACE

Markers (any comment style: Python `#`, JavaScript `//`, HTML `<!-- -->`, CSS `/* */`):

    edition:cut <why>      first line of a region that is removed (the marker lines themselves go too)
    edition:end            last line of that region
    #ic| <code>            a line that exists only in the IntelliCat edition: the prefix is removed there and the
    //ic| <code>           line is an ordinary comment everywhere else (the indentation before the prefix stays)
    <!--ic| <markup> -->   the same for HTML markup

Whole files that belong to the removed sources are listed in tools/edition_cut_files.txt (paths relative to the
output root). A region must leave valid code behind; the check at the end (compile + leftover scan) says if not.
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUFFIXES = (".py", ".js", ".html", ".css")
CUT = re.compile(r"edition:cut\b")
END = re.compile(r"edition:end\b")
ICONLY = re.compile(r"^(\s*)(?:#|//)ic\| ?(.*)$")
ICONLY_HTML = re.compile(r"^(\s*)<!--ic\| ?(.*?)\s*-->$")


def cut_files():
    out = []
    for line in open(os.path.join(HERE, "edition_cut_files.txt"), encoding="utf-8-sig"):
        line = line.split("#")[0].strip()
        if line:
            out.append(line)
    return out


def process_text(text, name):
    """Returns (new_text, removed_lines). Raises ValueError on unbalanced markers."""
    out, depth, removed, opened_at = [], 0, 0, 0
    for n, line in enumerate(text.splitlines(keepends=True), 1):
        if CUT.search(line):
            if depth:
                raise ValueError(f"{name}:{n}: nested edition:cut (opened at line {opened_at})")
            depth, opened_at = 1, n
            removed += 1
            continue
        if END.search(line):
            if not depth:
                raise ValueError(f"{name}:{n}: edition:end without edition:cut")
            depth = 0
            removed += 1
            continue
        if depth:
            removed += 1
            continue
        m = ICONLY.match(line.rstrip("\r\n")) or ICONLY_HTML.match(line.rstrip("\r\n"))
        if m:
            line = m.group(1) + m.group(2) + ("\n" if line.endswith("\n") else "")
        out.append(line)
    if depth:
        raise ValueError(f"{name}: edition:cut opened at line {opened_at} is never closed")
    return "".join(out), removed


def main(root):
    root = os.path.abspath(root)
    gone = 0
    for rel in cut_files():
        path = os.path.join(root, rel)
        if os.path.isdir(path):
            import shutil
            shutil.rmtree(path)
            gone += 1
        elif os.path.exists(path):
            os.remove(path)
            gone += 1
        else:
            print(f"warning: {rel} is listed in edition_cut_files.txt but is not in the output", file=sys.stderr)
    removed_total, files = 0, 0
    problems = []
    for base, _dirs, names in os.walk(root):
        if "node_modules" in base or "/vendor" in base.replace("\\", "/") or "__pycache__" in base:
            continue
        for fn in names:
            if not fn.endswith(SUFFIXES):
                continue
            path = os.path.join(base, fn)
            try:
                text = open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            if "edition:" not in text and "ic| " not in text:
                continue
            try:
                new, removed = process_text(text, os.path.relpath(path, root))
            except ValueError as e:
                problems.append(str(e))
                continue
            if new != text:
                open(path, "w", encoding="utf-8", newline="").write(new)
                files += 1
                removed_total += removed
    print(f"edition_build: {gone} files removed, {removed_total} lines cut in {files} files")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
