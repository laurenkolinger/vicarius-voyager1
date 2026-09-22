"""GPU right-of-way client: ask VICARIUS on port 5090 whether a card is free.

This file is VOYAGER 1's (3D_phase_1's) copy of the VICARIUS platform
reference implementation for the GPU right-of-way helper (spec docs/
superpowers/specs/2026-09-17-gpu-right-of-way-design.md, section "The helper
(copied into modules)"), copied verbatim from vicarius_ui_os/gpu_claim_client.py
except for this docstring. run_phase1.py calls check() once, immediately
after the command line is parsed and validated and before Metashape is ever
touched, so a launch from a terminal that bypasses the desktop UI's runner
still refuses to collide with another model-tier run. It never calls claim()
or release() itself: vicarius_ui_os/runner.py's start_job() already made the
binding claim, with this process's own pid, before spawning it, and releases
it when the job finishes. Other copies: vicarius_ui_os/gpu_claim_client.py
(the reference), 3D_phase2/src/gpu_claim.py, reef_point_seg's
pipeline_orchestrator/gpu_claim.py, vicarius_llm/src/gpu_claim.py and
driver/gpu_claim.py. This is the same copy-in pattern as lock_status.py in
reef_point_seg's pipeline_orchestrator.

The one claims registry is written only by vicarius_ui_os (the desktop UI on
port 5090); this helper never writes anything itself, it only asks over HTTP.
It never raises: an HTTP 409 or 428 from the server is a decision (refused,
or needs a PIN), not a failure, and is handed back to the caller exactly as
the server sent it. Only a connection failure, a timeout, or a reply this
helper cannot parse counts as a failure, and that failure is reported as
decision "unregistered": the caller proceeds with its work, and VICARIUS's
watcher will show the caller as an unclaimed holder once port 5090 answers
again. VICARIUS_GPU_BYPASS=1 skips the request altogether and answers
"unregistered" at once, for a manual dev run with no desktop UI up.

Stdlib only, so any module can embed this with zero dependencies: json, os,
sys, urllib.request, urllib.error.
"""

import json
import os
import sys
import urllib.error
import urllib.request

# The desktop UI's own base address. VICARIUS_UI_URL overrides it so a test,
# or a box where 5090 is not the desktop UI's port, can point elsewhere.
BASE_URL_ENV = "VICARIUS_UI_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:5090"
BASE_URL = os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL

# A manual dev run with no desktop UI up sets this to skip the network call
# entirely and proceed as "unregistered" (spec: "logged as such").
BYPASS_ENV = "VICARIUS_GPU_BYPASS"

# How long a socket operation may take before this helper gives up and
# treats port 5090 as unreachable.
TIMEOUT_S = 3

CHECK_PATH = "/api/gpu/check"
CLAIM_PATH = "/api/gpu/claim"
RELEASE_PATH = "/api/gpu/release"

# One stderr line per distinct warning, so a long-running caller (a batch
# loop, a kept-open module) does not spam its own log every time it asks.
_warned: set[str] = set()


def _warn_once(msg: str) -> None:
    """Print one stderr line for a distinct warning, matching lock_status.py.

    Parameters:
        msg: the warning text; a "gpu_claim: " prefix is added here so every
            call site keeps this consistent.

    Returns: None.
    """
    if msg not in _warned:
        _warned.add(msg)
        print(f"gpu_claim: {msg}", file=sys.stderr)


def _unregistered(context: str, reason: str) -> dict:
    """Build the "could not ask VICARIUS" fallback decision and warn once.

    Parameters:
        context: who was asking, for the warning: a module slug for check()
            and claim(), or a short label for release(), which carries no
            module of its own.
        reason: what went wrong, kept out of the canonical warning line and
            carried separately in the returned dict for the caller's own log.

    Returns: {"decision": "unregistered", "reason": reason}.
    """
    _warn_once(
        f"VICARIUS on 5090 did not answer; {context} is proceeding "
        f"unregistered and will show as unclaimed in the GPU monitor when "
        f"the UI returns ({reason})."
    )
    return {"decision": "unregistered", "reason": reason}


def _post(path: str, body: dict, context: str) -> dict:
    """POST one JSON body to the desktop UI and hand back its decision.

    An HTTP 409 or 428 (or any other status) whose body is a JSON object is
    a decision from the server, not a failure of this call, and is returned
    unchanged. A connection failure, a timeout, or a reply this function
    cannot parse as a JSON object falls back to decision "unregistered".

    Parameters:
        path: the route to POST to, one of CHECK_PATH, CLAIM_PATH,
            RELEASE_PATH.
        body: the JSON-serialisable request body.
        context: who is asking, used only in the "unregistered" warning.

    Returns: the parsed JSON object the server sent, or
    {"decision": "unregistered", "reason": "..."}. Never raises.
    """
    if os.environ.get(BYPASS_ENV) == "1":
        return _unregistered(context, f"{BYPASS_ENV}=1 is set")
    url = BASE_URL.rstrip("/") + path
    try:
        payload = json.dumps(body).encode("utf-8")
    except TypeError as exc:
        return _unregistered(context, f"could not encode the request body ({exc})")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError as decode_exc:
            return _unregistered(
                context, f"HTTP {exc.code} with an unreadable body ({decode_exc})")
        if isinstance(parsed, dict):
            return parsed
        return _unregistered(context, f"HTTP {exc.code} did not answer with a JSON object")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _unregistered(context, str(exc))
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return _unregistered(context, f"an unreadable reply ({exc})")
    if not isinstance(parsed, dict):
        return _unregistered(context, "a reply that was not a JSON object")
    return parsed


def check(module: str, tier: "str | None" = None, by: str = "LO") -> dict:
    """Ask VICARIUS whether `module` may claim a card right now.

    Advisory only: writes nothing to the claims registry.

    Parameters:
        module: the calling module's slug, for example "3D_phase_1".
        tier: the right-of-way tier to ask about; omit to let VICARIUS
            derive it from the module's entry in the ladder.
        by: initials of the person or process this check is made for.

    Returns: the decision dict VICARIUS sends, one of
    {"decision": "granted", "holders": [...], "message": "..."},
    {"decision": "refused", "holders": [...], "message": "..."},
    {"decision": "needs_pin", "holders": [...], "message": "..."}, or
    {"decision": "unregistered", "reason": "..."} when port 5090 could not
    be reached or its reply could not be read. Never raises.
    """
    body = {"module": module, "tier": tier, "by": by}
    return _post(CHECK_PATH, body, context=str(module))


def claim(module: str, tier: "str | None" = None, run_id: str = "",
          pid: "int | None" = None, by: str = "LO", bypass: bool = False,
          reason: str = "") -> dict:
    """Ask VICARIUS to record a claim on a card for `module`.

    Binding: on a "granted" decision, VICARIUS writes the claim to the
    registry (and sweeps any lower-tier holder) before answering.

    Parameters:
        module: the calling module's slug.
        tier: the right-of-way tier to claim; omit to let VICARIUS derive it
            from the module's entry in the ladder.
        run_id: the run's own identifier, shown to anyone this claim refuses.
        pid: the process id that will hold the card; VICARIUS requires it to
            be alive.
        by: initials of the person or process this claim is made for.
        bypass: True to use an admin PIN override on a "needs_pin" decision;
            VICARIUS requires a non-empty reason and an admin session when
            this is set.
        reason: the one-line reason shown and audited for a bypass.

    Returns: the decision dict VICARIUS sends, one of
    {"decision": "granted", "claim_id": "...", "swept": [...]},
    {"decision": "refused", "holders": [...], "message": "..."},
    {"decision": "needs_pin", "holders": [...], "message": "..."}, or
    {"decision": "unregistered", "reason": "..."} when port 5090 could not
    be reached or its reply could not be read. Never raises.
    """
    body = {"module": module, "tier": tier, "run_id": run_id, "pid": pid,
            "by": by, "bypass": bypass, "reason": reason}
    return _post(CLAIM_PATH, body, context=str(module))


def release(claim_id: "str | None" = None, pid: "int | None" = None) -> dict:
    """Tell VICARIUS a claim is finished, freeing the card for the next start.

    Parameters:
        claim_id: the id claim() returned; the usual way to release.
        pid: the process id the claim was made for, when the claim id was
            lost. At least one of claim_id or pid should identify the claim.

    Returns: the JSON object VICARIUS sends back (its /api/gpu/release route
    answers 200 when the claim existed and 404 otherwise, both as a JSON
    body), or {"decision": "unregistered", "reason": "..."} when port 5090
    could not be reached or its reply could not be read. Never raises.
    """
    body = {}
    if claim_id is not None:
        body["claim_id"] = claim_id
    if pid is not None:
        body["pid"] = pid
    context = f"the release of {claim_id if claim_id is not None else pid}"
    return _post(RELEASE_PATH, body, context=context)


class GpuHeldError(Exception):
    """Signals that a module's own client is deliberately refusing GPU work.

    check(), claim() and release() above never raise this or anything else;
    it exists for a module's higher-level code (for example Vesper.llm's
    OllamaClient, which holds itself idle rather than dying when swept) to
    carry the held notice through its own call stack as one exception type,
    so every caller of that code has one thing to catch.

    Parameters:
        notice: the message a person waiting on the call should see.
    """

    def __init__(self, notice: str = ""):
        super().__init__(notice)
        self.notice = notice
