"""Microagents whose build_prompt/verify CODE runs in Caroline's own
process (see app/microagent_client.py's own module docstring) rather than
being deployed into Camerlengo's server codebase. The actual model calls
still go through Camerlengo's microagent:call command -- only the
agent-specific logic lives here."""
