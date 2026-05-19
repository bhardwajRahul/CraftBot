"""Per-integration verification helpers for LIVE end-to-end tests.

Each integration module in here owns the knowledge of:
  - the integration's client + how to extract the owner's identifier
    (e.g. ``owner_phone`` for whatsapp_web, ``emailAddress`` for gmail).
  - how to query "did a recent outgoing message land in my own chat?"
    using the integration's native methods (``get_chat_messages`` for
    whatsapp, ``users.messages.list`` for gmail, etc.).

The generic infrastructure (``build_agent``, ``run_scenario``) lives in
[..._live_helpers.py](tests/e2e/_live_helpers.py); it stays integration-
agnostic. Tests import generic infra from there and integration-specific
verification from this package.

Adding support for a new integration is a single new module here exposing
``recent_messages_in_self_chat(*, since_ts, contains=None) -> list[dict]``
(or the equivalent verb for that integration's primary surface).
"""
