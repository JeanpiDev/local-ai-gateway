"""Barrido de la caché de auth: las entradas vencidas no se acumulan."""
from app import auth
from app.auth import AuthContext


def test_purge_expired_removes_only_stale(monkeypatch):
    auth._cache.clear()
    ctx = AuthContext(token="t", user={"id": "u"})
    auth._cache["viva"] = (ctx, 100.0)      # expira en t=100
    auth._cache["vencida"] = (ctx, 50.0)    # expira en t=50

    auth._purge_expired(now=60.0)           # t=60: solo "vencida" caduca

    assert "viva" in auth._cache
    assert "vencida" not in auth._cache
    auth._cache.clear()
