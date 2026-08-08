"""Adapters for the real services, and the switch that decides whether to use them.

Lifted from the first generation, which ran them in production. Plain `urllib`, module-level
functions, no client object to construct, no state except a cached Google client — which is
why they came across unchanged while nothing else in that generation's live layer could.

Every failure mode collapses to one exception, `LiveToolUnavailable`: unreachable, rejected,
credentials missing, library missing. The caller has one thing to catch and one thing to
know, which is that **it did not happen**.
"""

from bat.live.integrations.gate import LiveToolUnavailable, is_live, live_status

__all__ = ["LiveToolUnavailable", "is_live", "live_status"]
