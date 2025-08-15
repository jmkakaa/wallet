import sys
from pathlib import Path

import pytest
import httpx

sys.path.append(str(Path(__file__).resolve().parents[1]))
import backend


@pytest.mark.asyncio
async def test_me_endpoint_creates_user(tmp_path):
    backend.DB_PATH = str(tmp_path / "test.db")
    await backend.startup()
    user_id = 123456
    transport = httpx.ASGITransport(app=backend.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/me", params={"user_id": user_id})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == user_id
        assert data["balance"] == "0.00"
        conn = backend.app.state.db
        cursor = await conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        assert row is not None
    await backend.app.state.db.close()
