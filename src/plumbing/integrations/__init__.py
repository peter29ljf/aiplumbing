"""Real service adapters — Twilio, Gmail, Stripe.

These are complete and wired, but unreachable until two independent switches are
flipped in config/tool_catalog.yaml:

    live_tools_enabled: true      # master switch
    statuses: {sms.send: live}    # and the individual tool

Both are required. A tool marked `live` while the master switch is off still runs
against the simulator. The point is that no accident, typo or over-eager agent can
produce a real charge — it takes a deliberate edit in two places.
"""

from plumbing.integrations.gate import LiveToolUnavailable, is_live, live_status

__all__ = ["LiveToolUnavailable", "is_live", "live_status"]
