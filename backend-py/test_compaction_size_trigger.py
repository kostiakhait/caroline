"""The context-size compaction trigger (chat_session_mixins/compaction.py).
Run: python -m pytest test_compaction_size_trigger.py -q"""

from app.chat_session_mixins.compaction import CompactionMixin


class Tab(CompactionMixin):
    def __init__(self, tokens, baseline):
        self.last_known_context_tokens, self.tokens_at_last_forced_compaction = tokens, baseline


def test_a_large_context_triggers():
    assert Tab(200_000, None)._context_over_size_limit()
    assert Tab(200_000, 30_000)._context_over_size_limit()


def test_a_small_or_unknown_context_does_not():
    assert not Tab(120_000, None)._context_over_size_limit()
    assert not Tab(None, None)._context_over_size_limit()


def test_a_large_summary_does_not_loop():
    assert not Tab(175_000, 160_000)._context_over_size_limit()
