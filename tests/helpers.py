import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import accounts  # noqa: E402


def founder_client(server, name="boss", password="boss-pass1"):
    """A test client logged in as the founder (the owner: the first admin, made by setup). The data folder must already be patched."""
    accounts.add(name, password, "admin", founder=True)
    c = server.app.test_client()
    r = c.post("/login", data={"username": name, "password": password})
    assert r.status_code == 303, r.status_code
    return c
