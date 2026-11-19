"""Single login: master accounts get the admin console; clients can't reach admin APIs."""
import os

os.environ.update(ADMIN_PASSCODE="boss", APP_PASSCODE="client", ADMIN_USERS="admin,owner")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
s = get_settings()
s.admin_passcode, s.app_passcode, s.admin_users = "boss", "client", ["admin", "owner"]

from app import main  # noqa: E402

main.settings = s


def login(c, user, code):
    return c.post("/api/login", json={"username": user, "code": code})


def test_roles_and_admin_guard():
    with TestClient(main.app) as c:
        assert login(c, "admin", "client").status_code == 401          # master needs the admin code
        assert login(c, "rahul", "wrong").status_code == 401
        adm = login(c, "admin", "boss").json()
        usr = login(c, "rahul", "client").json()
        assert adm["role"] == "admin" and usr["role"] == "user"
        A = {"Authorization": f"Bearer {adm['token']}"}
        U = {"Authorization": f"Bearer {usr['token']}"}
        assert c.get("/api/admin/overview", headers=U).status_code == 403
        assert c.get("/api/admin/overview", headers=A).status_code == 200
        assert c.get("/api/portfolio", headers={"Authorization": "Bearer forged"}).status_code == 401
        # user prefs + admin kill switch flow into the risk engine
        assert c.put("/api/me/settings", headers=U, json={"risk_profile": "conservative"}).json()["risk_profile"] == "conservative"
        assert c.put("/api/admin/settings", headers=A, json={"trading_halted": True}).json()["trading_halted"] is True
        cfg = c.get("/api/config", headers=U).json()
        assert cfg["trading_halted"] and cfg["role"] == "user"
        c.put("/api/admin/settings", headers=A, json={"trading_halted": False})


def test_diagnostics_and_no_bare_500():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        A = {"Authorization": f"Bearer {login(c, 'admin', 'boss').json()['token']}"}
        d = c.get("/api/admin/diagnostics", headers=A).json()
        by = {r["check"]: r for r in d["results"]}
        for name in ("Database (Neon)", "Quote: NSE stock", "Quote: MCX commodity (proxy)", "Daily candles",
                     "Technical analysis", "Macro regime", "Fundamentals"):
            assert by[name]["ok"], by[name]
        assert len(d["results"]) == 14

        @main.app.get("/api/_boom_test")
        async def boom():
            raise ValueError("kaboom")

        r = c.get("/api/_boom_test")
        assert r.status_code == 500 and "ValueError: kaboom" in r.json()["detail"]
