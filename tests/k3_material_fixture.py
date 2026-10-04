"""Anonymous real-case-derived dimensions; no K3 transcript text or identifiers.

Root-only profiling supplied K3-01 initial adaptation, K3-02 benchmark/checkpoint
correction, K3-03 repeated prefix-cache progress with pending outcome, and K3-04
resumed precision debugging. The text below is reconstructed, not a quotation.
"""
from mindie_knowledge.materials import summarizer

FACTS = {
    "K3-01": "Initial model adaptation hypothesis; no independent confirmation.",
    "K3-02": "A middle checkpoint correction changes the earlier benchmark interpretation.",
    "K3-03": "Repeated prefix-cache progress still leaves the reported outcome pending.",
    "K3-04": "Precision debugging resumes in the same task after an interruption.",
}


def request(worker, case="K3-01", previous=None):
    block = dict(block_id=summarizer.digest([case, "new-block"]), text=FACTS[case],
                 source_range=dict(case=case, reconstructed=True))
    return summarizer.make_request(task_id=summarizer.digest(case), body_version=summarizer.digest([case, "current"]),
                                  blocks=[block], prior_navigation=previous, identity=worker.identity())


def result(payload, case="K3-01"):
    return dict(blocks=[dict(block_id=block["block_id"], title=case, summary=FACTS[case]) for block in payload["blocks"]],
                navigation=dict(title=case, summary=FACTS[case]))


def completion(receipt):
    receipt.update(native_started=True, turn_started=True, turn_completed=True,
                   usage=dict(input_tokens=120, cached_input_tokens=20, output_tokens=30), elapsed_ms=10)


def summary_command(title="Synthetic summary", summary="Reported public observations."):
    """A process-protocol double; it does not claim real model quality."""
    import json
    from pathlib import Path
    import sys
    return [sys.executable, str(Path(__file__).resolve()), json.dumps(dict(title=title, summary=summary))]


if __name__ == "__main__":
    import json
    import sys
    if "--identity" in sys.argv:
        response = summarizer.policy_identity(model="synthetic", effort="low", implementation={"fixture": "complete-public-blocks"})
    else:
        payload = json.load(sys.stdin)
        navigation = json.loads(sys.argv[1])
        metadata = dict(blocks=[dict(block_id=block["block_id"], **navigation) for block in payload["blocks"]],
                        navigation=navigation)
        response = summarizer.outcome(payload, status="returned", result=metadata,
            raw_result=json.dumps(metadata), model_calls=1, usage_known=True,
            usage=dict(input_tokens=120, cached_input_tokens=0, output_tokens=30))
    print(json.dumps(response))
