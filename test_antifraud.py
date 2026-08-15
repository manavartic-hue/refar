import asyncio
import importlib.util
import os
import tempfile
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi.testclient import TestClient


def make_init_data(bot_token: str, user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Endpoint"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


async def run() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["DB_PATH"] = os.path.join(temp_dir, "bot.db")
        os.environ["DEVICE_BINDING_SECRET"] = "test-only-secret-change-in-production"
        os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN_FOR_LOCAL_TESTS_ONLY"

        spec = importlib.util.spec_from_file_location("bot", "bot.py")
        bot = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(bot)

        await bot.init_db()
        await bot.set_setting("device_verify_enabled", "1")
        await bot.set_setting("duplicate_device_referral_block_enabled", "1")

        await bot.create_user(1001, "referrer", "Referrer", None)
        await bot.create_user(1002, "duplicate", "Duplicate", 1001)
        await bot.create_user(1003, "unique", "Unique", 1001)
        await bot.create_user(1004, "endpoint", "Endpoint", None)

        await bot.bind_verified_device(1001, "A" * 43, "198.51.100.10", "test-agent")
        await bot.bind_verified_device(1002, "A" * 43, "198.51.100.10", "test-agent")
        blocked = await bot.credit_referral_and_mark(1002, 1001)
        assert blocked is False
        assert await bot.get_referral_decision(1002) == ("BLOCKED", "SAME_DEVICE_MULTIPLE_ACCOUNTS")

        await bot.bind_verified_device(1003, "B" * 43, "198.51.100.20", "test-agent")
        approved = await bot.credit_referral_and_mark(1003, 1001)
        assert approved is True
        assert await bot.get_referral_decision(1003) == ("APPROVED", "")

        referrer = await bot.get_user(1001)
        assert referrer is not None and referrer["referral_count"] == 1

        with TestClient(bot.web_app) as client:
            assert client.get("/health").json()["ok"] is True
            response = client.post(
                "/api/verify",
                json={
                    "initData": make_init_data(os.environ["BOT_TOKEN"], 1004),
                    "bindingId": "C" * 43,
                },
            )
            assert response.status_code == 200 and response.json()["status"] == "verified"
        endpoint_user = await bot.get_user(1004)
        assert endpoint_user is not None and endpoint_user["device_verified"] == 1


if __name__ == "__main__":
    asyncio.run(run())
    print("anti-fraud checks passed")
