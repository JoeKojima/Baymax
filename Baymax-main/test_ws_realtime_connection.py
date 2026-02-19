import os
import asyncio
import json
import websockets
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = "gpt-realtime"
WS_URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"

async def main():
    print("Connecting to:", WS_URL)
    headers = [
        ("Authorization", f"Bearer {API_KEY}"),
        ("OpenAI-Beta", "realtime=v1"),
    ]
    async with websockets.connect(
        WS_URL,
        additional_headers=headers,
        ping_interval=20,
    ) as ws:
        print("Connected! Sending test request...")
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {"instructions": "Say hello briefly."}
        }))
        async for msg in ws:
            print("RECV:", msg)

asyncio.run(main())
