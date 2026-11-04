#!/usr/bin/env python3
"""List every tr("...") string in the plugin, write locale/template.json, and
report strings missing from (or no longer used by) each translation catalog.

    python3 tools/check_strings.py
"""

import ast
import json
import pathlib
import sys

PKG = pathlib.Path(__file__).resolve().parent.parent / "RecorderPlus"
LOCALE = PKG / "locale"
# Strings passed to tr() through a variable rather than a literal.
EXTRA = ["Full", "Half", "Quarter", "High", "Balanced", "Small file"]


def strings():
    found = []
    for path in sorted(PKG.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                found.append(node.args[0].value)
    return list(dict.fromkeys(found + EXTRA))


def main():
    keys = strings()
    LOCALE.mkdir(exist_ok=True)
    (LOCALE / "template.json").write_text(
        json.dumps({k: "" for k in keys}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(keys)} strings -> locale/template.json")
    bad = False
    for cat in sorted(LOCALE.glob("*.json")):
        if cat.name == "template.json":
            continue
        data = json.loads(cat.read_text(encoding="utf-8"))
        missing = [k for k in keys if not data.get(k)]
        unused = [k for k in data if k not in keys]
        fields = [k for k in keys if data.get(k) and
                  sorted(ast.literal_eval(repr(set(_fields(k))))) != sorted(set(_fields(data[k])))]
        print(f"{cat.stem:6} missing {len(missing):3}  unused {len(unused):3}  placeholder mismatches {len(fields)}")
        for k in missing + unused + fields:
            print("   ", repr(k))
        bad |= bool(missing or fields)
    return 1 if bad else 0


def _fields(s):
    import string
    return [f for _, f, _, _ in string.Formatter().parse(s) if f]


if __name__ == "__main__":
    sys.exit(main())
