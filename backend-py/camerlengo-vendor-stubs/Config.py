"""Safe, secret-free Config.py replacement used ONLY when vendoring Camerlengo's
AI.py (+ its own small dependency closure: Cache.py/EmailBasics.py/Logger.py/
JSONStorage.py) into Caroline's own install for the small-model primary path
(see backend-py/app/small_model_engine.py). The Makefile copies AI.py/Cache.py/
EmailBasics.py/Logger.py/JSONStorage.py VERBATIM from reforce's own `caroline`
branch, but replaces Config.py with THIS file instead of copying the real one --
reforce's real Config.py holds live Partners Solutions production secrets
(admin/DB/email passwords, a Google Maps key, etc.) entirely unrelated to this
feature; shipping it in a public installer would leak them in cleartext.

Every value below exists ONLY because AI.py (or one of the four files above)
references Config.<name> somewhere -- confirmed by grepping the exact dependency
closure this vendors, nothing broader. None of the real secrets from reforce's
own Config.py appear anywhere in that closure at all (checked directly) -- the
only two attributes that WOULD be real credentials elsewhere (OPENAI_KEY,
OPENROUTER_KEY) are deliberately left blank here: Caroline's own
small_model_engine.py NEVER relies on AI.py's own default-adapter construction
(it always builds OpenRouterAdapter(api_key=...) with its own freshly-fetched,
per-session key from model_key_provisioning.py) -- these two only need to exist
so `AI.__init__`/`Adapter.__init__` don't raise AttributeError; their value is
never actually used by anything this vendored copy runs for Caroline.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE = os.path.join(BASE_DIR, ".data", "storage")
os.makedirs(STORAGE, exist_ok=True)

LOGGING_FOLDER = "logs"

# Never used for a real call from this vendored copy -- see module docstring.
OPENAI_KEY = ""
OPENROUTER_KEY = ""

# Real, non-secret model-routing values -- must match reforce's own Config.py
# so "SMALL"/"LARGE"/"ALTERNATE" resolve to the same actual model ids.
AI_MODEL_LARGE = "openai/gpt-5.1"
AI_MODEL_ALTERNATE = "meta-llama/llama-4-maverick"
AI_MODEL_SMALL = "openai/gpt-5-nano"
AI_MODEL_CODE_LARGE = "anthropic/claude-sonnet-4.5"
AI_FLEXIBLE_MODEL_USE = True
AI_BALANCE_THRESHOLD_TRANSLATION = 3.0

# None disables AI.py's own optional call-log/billing-log writers entirely
# (both already default to this via getattr(Config, "...", None) -- listed
# here for clarity, not strictly required).
AI_CALL_LOG_DIR = None

# TTS -- only exercised if Caroline's own small-model turn ever calls a TTS
# helper directly (it doesn't today; Caroline's own local_tts_server.py
# handles voice output), kept for import-time completeness only.
TTS_SERVER = ""
TTS_EDGE_NO_AUDIO_BACKOFF_SEC = 5.0
TTS_SILERO_VOICES = {
    "russian": {"female": "xenia", "male": "aidar"},
    "english": {"female": "en_0", "male": "en_1"},
}

# Camerlengo's own internal API self-call auth (Cache.py's cache-eviction
# helper reads Config.getSiteConfig for site-scoped cache limits, EmailBasics.py
# reads these EMAIL_* constants) -- none of this is exercised by Caroline's
# small-model path (it never sends email or touches Cache's site-config
# lookups), so these exist purely so `import AI` succeeds; harmless
# placeholders, never real credentials.
api_keys = []
EMAIL_SERVER = ""
EMAIL_PORT = "587"
EMAIL_USER = ""
EMAIL_PASSWORD = ""
EMAIL_SOCKET_TIMEOUT = 5
EMAIL_PS_API = ""


def getSiteConfig(var, site=None):
    """Cache.py's own site-scoped config lookup -- only called from cache-
    eviction bookkeeping this vendored copy's callers never trigger. Safe
    defaults so it never raises if somehow reached."""
    defaults = {"max_cache_size": 25 * 1024 * 1024, "disable_cache": []}
    return defaults.get(var)
