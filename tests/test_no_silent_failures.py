#!/usr/bin/env python3
"""
Module:  tests/test_no_silent_failures.py
Purpose: No broad exception in the run path may be caught and left unsaid.
Inputs:  src/*.py.
Outputs: pass/fail lines; exit 1 on the first failure.

2026-09-06. A run reported "No videos to extract frames from" while every
video sat on the disk. The real fault was an import error, caught into a
variable nothing read. The program knew and did not say, and finding out took
twenty minutes of reading source instead of one line of a log.

This guard holds the shape of that mistake rather than the instance of it. A
handler for a broad exception (Exception, BaseException, or a bare except) in
this module's source must do one of three things: log it, print it, or raise.
Returning a default quietly is how a caught failure becomes a wrong answer.

A narrow catch (ValueError, OSError, KeyError and friends) is exempt: those
are the expected shape of a specific input and returning a default is the
point. A handler that must stay silent carries `# silent-ok: <why>` on its except
line; the guard reads the reason there and refuses an empty one.
"""
import ast
import os
import pathlib
import sys

SRC = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "src"
BROAD = {"Exception", "BaseException"}
# Names that count as saying something. `log(` covers the local helper several
# of these files define; `notes.append` covers inspect_psx, which records every
# failure into the notes it returns to its caller rather than logging.
SPEAKS = ("logging", "logger", "print", "warn", "applog", "sys.stderr")
SPEAKING_CALLS = ("log", "_log", "log_message", "record", "note")
SPEAKING_ATTRS = ("append",)

# A handler that must stay silent says why on its own except line, as
# `except Exception:  # silent-ok: <reason>`. The reason travels with the code
# instead of living in a line-number table here, which broke every time a
# docstring above the handler grew (2026-09-11).
MARKER = "# silent-ok:"

FAILED = []


def check(name, ok, detail=""):
    if ok:
        print("ok   " + name)
    else:
        print("FAIL " + name + " " + str(detail))
        FAILED.append(name)


def _is_broad(handler):
    t = handler.type
    if t is None:
        return True                                   # bare except
    if isinstance(t, ast.Name):
        return t.id in BROAD
    if isinstance(t, ast.Tuple):
        return any(isinstance(e, ast.Name) and e.id in BROAD for e in t.elts)
    return False


def _speaks(handler):
    """True when the handler logs, prints, records or re-raises.

    A handler that does none of those turns a failure into a wrong answer
    with nobody told, which is the bug this guard exists for.
    """
    module = ast.Module(body=handler.body, type_ignores=[])
    dumped = ast.dump(module)
    if any(k in dumped for k in SPEAKS):
        return True
    for node in ast.walk(module):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in SPEAKING_CALLS:
                return True
            if isinstance(fn, ast.Attribute) and fn.attr in SPEAKING_CALLS + SPEAKING_ATTRS:
                return True
        # Recording the failure into a structure the caller reads counts too:
        # inspect_psx writes it into the document it returns rather than logging.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    return True
    return False


def _marked_silent(source_line):
    """True when the except line carries the marker and a non-empty reason.

    `except Exception:  # silent-ok: counting frames for a progress line`
    passes; a bare `# silent-ok:` with nothing after it does not, because a
    reason nobody wrote is not a reason.
    """
    if MARKER not in source_line:
        return False
    return bool(source_line.split(MARKER, 1)[1].strip())


offenders = []
for path in sorted(SRC.glob("*.py")):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        check("every source file parses", False, f"{path.name}: {exc}")
        continue
    lines = path.read_text(encoding="utf-8").splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_broad(node):
            continue
        if _marked_silent(lines[node.lineno - 1]):
            continue
        if not _speaks(node):
            offenders.append(f"{path.name}:{node.lineno}")

check("no broad exception is caught and left unsaid in src/",
      not offenders,
      "these catch Exception and neither log, print nor raise: " + ", ".join(offenders))

# The specific two fixed on 2026-09-06 stay fixed.
rp = (SRC / "run_phase1.py").read_text(encoding="utf-8")
check("the disk floor says when it falls back to the default",
      "built-in default" in rp)
check("a registry that cannot be read while settling a stage is reported",
      "could not be settled because the registry" in rp)

# And the client that started it all.
rc = (SRC / "registry_client.py").read_text(encoding="utf-8")
check("the registry client logs an import failure", "logging.error" in rc)
check("the registry client can be asked why it is unavailable", "def unavailable_reason" in rc)

print()
if FAILED:
    print(str(len(FAILED)) + " FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("no silent failures in the run path")
