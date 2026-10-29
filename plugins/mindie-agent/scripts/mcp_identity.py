"""Native Codex request identity and turn-scoped interrupt protocol.

[Role]
Validates the host-provided task/turn metadata and signals matching in-flight
requests for native interruption.

[Invariants]
- Thread identity comes only from consistent native metadata, never arguments
  or a parent task's knowledge grant.
- Interrupts only signal calls already registered for that exact thread/turn;
  they never claim that execution or cleanup completed.

[Timeouts & Locks]
- Pure in-memory validation; the MCP server owns the pending-call lock and
  process cancellation deadlines.

[Side-Effect Policy]
- Sets cancellation events for matching pending calls; no disk, service, or
  remote writes.
"""
from session_gate import IDENTITY


def native_identity(request):
    """Bind one tools/call to the native thread, not the session tree.

    Nested thread_id, nested session_id, and top-level threadId are required
    valid identity strings. thread_id must equal threadId. session_id names
    the session tree and is not required to equal the thread. Top-level
    sessionId is optional; when the key is present, null included, it must
    be a valid id equal to nested session_id. The 0.153.4 root probe showed
    equal ids on that one root task. It does not establish child metadata.
    Tool arguments and a parent lease are never fallbacks.
    """
    params = request.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("arguments"), dict):
        raise ValueError("Invalid MindIE tool arguments")
    meta = params.get("_meta")
    turn = meta.get("x-codex-turn-metadata") if isinstance(meta, dict) else None
    thread = turn.get("thread_id") if isinstance(turn, dict) else None
    session = turn.get("session_id") if isinstance(turn, dict) else None
    plain = meta.get("threadId") if isinstance(meta, dict) else None
    if not all(isinstance(value, str) and IDENTITY.fullmatch(value)
               for value in (thread, session, plain) if value is not None):
        raise ValueError("Invalid native task identity metadata")
    if thread is None or session is None or plain is None:
        raise ValueError(
            "This host does not deliver native task identity metadata; MindIE "
            "requires a Codex version with turn metadata on tools/call and "
            "fails closed here — do not retry or supply an identity by hand"
        )
    if thread != plain:
        raise ValueError(
            "Contradictory native task identity metadata; call rejected"
        )
    if isinstance(meta, dict) and "sessionId" in meta:
        top_session = meta.get("sessionId")
        if not isinstance(top_session, str) or not IDENTITY.fullmatch(top_session):
            raise ValueError("Invalid native task identity metadata")
        if top_session != session:
            raise ValueError(
                "Contradictory native task identity metadata; call rejected"
            )
    return thread


def native_turn_identity(request):
    thread = native_identity(request)
    turn = request["params"]["_meta"]["x-codex-turn-metadata"].get("turn_id")
    if not isinstance(turn, str) or not IDENTITY.fullmatch(turn):
        raise ValueError("Invalid native turn identity metadata")
    return thread, turn


def interrupt_turn(request, pending):
    """Signal existing calls only; the native Hook does not assert completion."""
    params = request.get("params")
    arguments = params.get("arguments") if isinstance(params, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(arguments, dict) or set(arguments) != {"thread_id", "turn_id"}:
        raise ValueError("Invalid native interrupt identity")
    scope = arguments["thread_id"], arguments["turn_id"]
    if not all(isinstance(value, str) and IDENTITY.fullmatch(value) for value in scope):
        raise ValueError("Invalid native interrupt identity")
    if not isinstance(meta, dict) or meta.get("threadId") != scope[0]:
        raise ValueError("Contradictory native interrupt identity")
    if "x-codex-turn-metadata" in meta and native_turn_identity(request) != scope:
        raise ValueError("Contradictory native interrupt identity")
    for event, identity in pending.values():
        if identity == scope:
            event.set()
    return dict(content=[dict(type="text", text="{}")], isError=False)
