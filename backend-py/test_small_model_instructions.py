"""Tripwire for small_model_engine.py's _derive_shared_always_on_instructions()
(2026-10-15). _SHARED_ALWAYS_ON_INSTRUCTIONS used to be a hand-maintained
tuple that silently drifted out of sync with policies.ALWAYS_ON_INSTRUCTIONS
-- four entries were missing for months before anyone noticed (see that
derivation's own comment for the real incident this traces to). Rewritten as
an actual derivation (ALWAYS_ON_INSTRUCTIONS minus a named exclusion/
substitution map) flips the failure mode from "silently missing" to "silently
included" -- safer, but a genuinely CLI-only future instruction could still
slip in unnoticed if it isn't added to the maps. This script catches that:
every function the sw engine actually ends up with is scanned for a short
deny-list of CLI-native-only tokens, and every map key is checked against
still being a real member of ALWAYS_ON_INSTRUCTIONS (keeps the maps from
going stale in the other direction, if an instruction is ever removed or
renamed upstream).

Run directly: python test_small_model_instructions.py
"""

from __future__ import annotations

import re
import sys

from app import policies
from app import small_model_engine as sme

# Case-insensitive; \b-bounded where it matters so "Task" doesn't flag
# ordinary prose use of the word "task" itself.
_CLI_ONLY_TOKENS = re.compile(
    r"\bTodoWrite\b|\brun_in_background\b|\bBashOutput\b|\bTask\b\s+(?:subagent|tool|if available)|delegate to a subagent via Task",
    re.IGNORECASE,
)


def check_no_cli_only_tokens() -> list[str]:
    failures = []
    for fn in sme._SHARED_ALWAYS_ON_INSTRUCTIONS:
        text = fn()
        match = _CLI_ONLY_TOKENS.search(text)
        if match:
            failures.append(
                f"{fn.__name__} (in the sw instruction set) contains CLI-only token {match.group(0)!r} -- "
                "add it to _SW_VEHICLE_SUBSTITUTIONS or _CLI_NATIVE_ONLY_EXCLUSIONS in small_model_engine.py."
            )
    return failures


def check_maps_reference_real_instructions() -> list[str]:
    failures = []
    live = set(policies.ALWAYS_ON_INSTRUCTIONS)
    for fn in list(sme._SW_VEHICLE_SUBSTITUTIONS) + list(sme._CLI_NATIVE_ONLY_EXCLUSIONS):
        if fn not in live:
            failures.append(
                f"{fn.__name__} is referenced in small_model_engine.py's substitution/exclusion maps but is no "
                "longer in policies.ALWAYS_ON_INSTRUCTIONS -- remove the stale entry."
            )
    return failures


def check_known_entries_present() -> list[str]:
    """Regression guard for the specific drift this fix closed (2026-10-15):
    four entries that were silently missing from the old hand-maintained
    tuple for months before anyone noticed."""
    names = {fn.__name__ for fn in sme._SHARED_ALWAYS_ON_INSTRUCTIONS}
    expected = {
        "no_update_sentinel_instruction",
        "prefer_command_line_and_scripting_instruction",
        "document_opening_preference_instruction",
        "no_unrequested_send_confirmation_instruction",
        "complex_task_execution_instruction_for_sw",
    }
    missing = expected - names
    return [f"{name} is expected in the sw instruction set but is missing." for name in sorted(missing)]


def main() -> int:
    failures = check_no_cli_only_tokens() + check_maps_reference_real_instructions() + check_known_entries_present()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: {len(sme._SHARED_ALWAYS_ON_INSTRUCTIONS)} instructions checked, no CLI-only tokens, no stale map entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
