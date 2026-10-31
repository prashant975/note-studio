"""app.py, booted with authentication satisfied, for Streamlit's AppTest.

Not a test. This is the script AppTest runs: it stands in for a signed-in,
whitelisted @pw.live user so the page under test is the real one rather than the
login screen.

Every patch here replaces a NETWORK call and nothing else — Google identity and
the PW proxy's whitelist. The page, its widgets, its panels and all of their
branching run exactly as they do in production, which is the whole point:
app.py's panels were previously "tested" by searching its source text for
substrings, so a panel could be syntactically present and completely broken and
36 assertions would still pass.
"""
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

import pw_access
from src import proxy_session


class _SignedInUser(dict):
    """What `st.user` looks like after a successful Google sign-in.

    A dict subclass because app.py reads it both ways: `st.user.get("email")`
    and `st.user.is_logged_in`.
    """

    is_logged_in = True

    def __init__(self) -> None:
        super().__init__(
            is_logged_in=True,
            email="tester@pw.live",
            tokens={"id": "test-id-token", "access": "test-access-token"},
        )


st.user = _SignedInUser()

# The proxy is the single source of truth for authorization and it fails closed,
# so without these the page under test is the "not authorized" screen. Patched
# at the module attributes app.py actually calls.
pw_access.check_allowed = lambda *a, **k: True
pw_access.check_allowed_status = lambda *a, **k: "allowed"
proxy_session.attach = lambda *a, **k: None
proxy_session.persist = lambda *a, **k: None

runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
