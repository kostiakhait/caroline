"""Template for sw_service_keys_local.py (gitignored, never committed).

Copy this file to sw_service_keys_local.py next to it and fill in real
values issued via Camerlengo's user:issueKey (scope_or_superadmin auth --
needs a superadmin SquirrelWisdom session). Every module that used to
hardcode one of these constants now imports it from sw_service_keys_local
instead -- see sw_api.py, subscription_mode.py, email_plugin.py,
ratatosk_own_account.py, reforce_v2.py.

Background (2026-10): the previous values were committed in plaintext to
this public repo (sw_api.py:27 and friends) -- a third-party security
read of the vladyslav-zasiienko branch flagged it. All five were rotated
on Camerlengo; the old values are revoked or scheduled for revocation
after other live Caroline installs get a build with the new ones.
"""

CAROLINE_SW_KEY = "REPLACE_ME"
V2_LOGIN_SERVICE_KEY = "REPLACE_ME"
V2_SESSION_LOOKUP_KEY = "REPLACE_ME"
EMAIL_SERVICE_KEY = "REPLACE_ME"
REFORCE_KEY = "REPLACE_ME"
