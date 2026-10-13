"""Existing runtime subscriptions to neutral context commits, unchanged by Phase A.

Wiring is outside the intelligence layer. Empty/injected subscribers allow standalone
context ingestion. Existing workers retain their legacy wakeup/enrollment behavior.
"""

from market_signal.context.work_monitors import PROVIDER_IDS


def gateway_consumers(store, completion, receipt):
    def consume(_publication):
        from market_signal.ops.evaluation_triggers import gateway_wakeup

        gateway_wakeup(store, completion, receipt["payload"])
        if completion.get("provider") in PROVIDER_IDS:
            from market_signal.context.work_research import enroll

            enroll(store, completion, receipt)

    return (consume,)


def free_source_consumers(store, completion, observation):
    def consume(_publication):
        from market_signal.context.work_research import enroll
        from market_signal.ops.evaluation_triggers import gateway_wakeup

        gateway_wakeup(store, completion, {})
        enroll(
            store,
            completion,
            {
                "receipt_id": completion["receipt_id"],
                "auth_identity": "public_free_source",
                "gateway_received_at": completion["gateway_received_at"],
                "payload": {"item": observation},
            },
            origin="free",
        )

    return (consume,)
