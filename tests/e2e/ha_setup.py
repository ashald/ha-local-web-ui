"""Configure a running Home Assistant for the Local Web UIs end-to-end test.

- onboards it if needed (user demo / demo-password-123) and writes tokens to <tokens.json>
- removes the variant 1 PoC integration (esphome_web_ui) if present
- (re)adds the ESPHome device at <device host>:6053
- adds the Local Web UIs hub, accepts the device's discovered web UI, and adds a web UI
  for the stand-in router by URL (with stored login)

Usage: python ha_setup.py <tokens.json> <device host>
"""

import asyncio
import json
import pathlib
import sys

import aiohttp

HA = "http://127.0.0.1:8123"
CLIENT_ID = f"{HA}/"


async def main(token_file: str, device_host: str) -> None:
    path = pathlib.Path(token_file)
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{HA}/api/onboarding") as r:
            # The onboarding API goes away (404) once onboarding is done
            needs_onboarding = r.status == 200 and not (await r.json())[0]["done"]
        if needs_onboarding:
            async with http.post(
                f"{HA}/api/onboarding/users",
                json={
                    "client_id": CLIENT_ID,
                    "name": "Demo",
                    "username": "demo",
                    "password": "demo-password-123",
                    "language": "en",
                },
            ) as r:
                code = (await r.json())["auth_code"]
            async with http.post(
                f"{HA}/auth/token",
                data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID},
            ) as r:
                tokens = await r.json()
            auth = {"Authorization": f"Bearer {tokens['access_token']}"}
            for step in ("core_config", "analytics"):
                await http.post(f"{HA}/api/onboarding/{step}", headers=auth, json={})
            await http.post(
                f"{HA}/api/onboarding/integration",
                headers=auth,
                json={"client_id": CLIENT_ID, "redirect_uri": f"{HA}/?auth_callback=1"},
            )
            tokens.update({"hassUrl": HA, "clientId": CLIENT_ID})
            path.write_text(json.dumps(tokens), encoding="utf-8")  # noqa: ASYNC240 - test script
        tokens = json.loads(path.read_text(encoding="utf-8"))  # noqa: ASYNC240 - test script
        async with http.post(
            f"{HA}/auth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": CLIENT_ID,
            },
        ) as r:
            auth = {"Authorization": f"Bearer {(await r.json())['access_token']}"}

        async with http.get(f"{HA}/api/config/config_entries/entry", headers=auth) as r:
            entries = await r.json()
        for entry in entries:
            if entry["domain"] in ("esphome_web_ui", "esphome", "local_web_ui"):
                await http.delete(
                    f"{HA}/api/config/config_entries/entry/{entry['entry_id']}", headers=auth
                )
                print("removed", entry["domain"], entry["title"])

        async def flow(handler: str, *steps: dict) -> dict:
            async with http.post(
                f"{HA}/api/config/config_entries/flow",
                headers=auth,
                json={"handler": handler, "show_advanced_options": False},
            ) as r:
                result = await r.json()
            for step in steps:
                async with http.post(
                    f"{HA}/api/config/config_entries/flow/{result['flow_id']}",
                    headers=auth,
                    json=step,
                ) as r:
                    result = await r.json()
            print(
                handler,
                "->",
                result.get("type"),
                result.get("title") or result.get("reason") or result.get("errors"),
            )
            return result

        await flow("esphome", {"host": device_host, "port": 6053})
        # The hub (options for all web UIs), then the ESPHome device's web UI,
        # which discovery offers, and a router added by URL
        await flow("local_web_ui", {})
        token = auth["Authorization"].split()[1]
        async with http.ws_connect(f"{HA}/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            await ws.receive_json()
            for attempt in range(20):
                await ws.send_json({"id": attempt + 1, "type": "config_entries/flow/progress"})
                offers = [
                    f
                    for f in (await ws.receive_json())["result"]
                    if f["handler"] == "local_web_ui"
                    and f["context"].get("source") == "integration_discovery"
                ]
                if offers:
                    break
                await asyncio.sleep(0.5)
        for offer in offers:
            async with http.post(
                f"{HA}/api/config/config_entries/flow/{offer['flow_id']}", headers=auth, json={}
            ) as r:
                print("discovered ->", (await r.json()).get("title"))
        await flow(
            "local_web_ui",
            {
                "name": "Router",
                "url": f"http://{device_host}:8081/",
                "mode": "isolated",
                "trusted_acknowledged": False,
                "verify_ssl": True,
                "username": "admin",
                "password": "secret",
                "show_in_sidebar": True,
                "icon": "mdi:router-wireless",
            },
        )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
