import asyncio
import base64
import json
import websockets
import ssl
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from typing import Dict, Any
from twilio.twiml.voice_response import VoiceResponse, Connect

app = FastAPI()

class DeepgramSTSHandler:
    def __init__(self, deepgram_api_key: str):
        self.deepgram_api_key = deepgram_api_key
        self.audio_queue = asyncio.Queue()
        self.streamsid_queue = asyncio.Queue()

    async def connect_sts(self) -> websockets.WebSocketClientProtocol:
        """Establish connection to Deepgram STS WebSocket"""
        extra_headers = {"Authorization": f"Token {self.deepgram_api_key}"}
        return await websockets.connect(
            "wss://sts.sandbox.deepgram.com/agent",
            extra_headers=extra_headers
        )

    async def send_sts_configuration(self, sts_ws: websockets.WebSocketClientProtocol):
        """Send initial configuration to STS"""
        config_message = {
            "type": "SettingsConfiguration",
            "audio": {
                "input": {
                    "encoding": "mulaw",
                    "sample_rate": 8000,
                },
                "output": {
                    "encoding": "mulaw",
                    "sample_rate": 8000,
                    "container": "none",
                },
            },
            "agent": {
                "listen": {"model": "nova-2"},
                "think": {
                    "provider": {
                        "type": "anthropic",
                    },
                    "model": "claude-3-haiku-20240307",
                    "instructions": "You are a helpful car seller.",
                },
                "speak": {"model": "aura-asteria-en"},
            },
        }
        await sts_ws.send(json.dumps(config_message))

    async def sts_sender(self, sts_ws: websockets.WebSocketClientProtocol):
        """Send audio chunks from queue to STS"""
        while True:
            chunk = await self.audio_queue.get()
            await sts_ws.send(chunk)

    async def sts_receiver(self, sts_ws: websockets.WebSocketClientProtocol, twilio_ws: WebSocket):
        """Receive messages from STS and handle them"""
        streamsid = await self.streamsid_queue.get()

        async for message in sts_ws:
            if isinstance(message, str):
                # Handle text messages (like barge-in events)
                decoded = json.loads(message)
                if decoded['type'] == 'UserStartedSpeaking':
                    await twilio_ws.send_json({
                        "event": "clear",
                        "streamSid": streamsid
                    })
                continue

            # Send TTS audio back to Twilio
            raw_mulaw = message
            await twilio_ws.send_json({
                "event": "media",
                "streamSid": streamsid,
                "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")}
            })

    async def twilio_receiver(self, twilio_ws: WebSocket):
        """Receive and process Twilio WebSocket messages"""
        BUFFER_SIZE = 20 * 160
        inbuffer = bytearray(b"")

        try:
            while True:
                data = await twilio_ws.receive_json()

                if data["event"] == "start":
                    streamsid = data["start"]["streamSid"]
                    await self.streamsid_queue.put(streamsid)

                if data["event"] == "media":
                    media = data["media"]
                    chunk = base64.b64decode(media["payload"])
                    if media["track"] == "inbound":
                        inbuffer.extend(chunk)

                # Process buffer in chunks
                while len(inbuffer) >= BUFFER_SIZE:
                    chunk = inbuffer[:BUFFER_SIZE]
                    await self.audio_queue.put(chunk)
                    inbuffer = inbuffer[BUFFER_SIZE:]

                if data["event"] == "stop":
                    break

        except WebSocketDisconnect:
            # Handle WebSocket disconnection
            pass

    async def handle_twilio_connection(self, twilio_ws: WebSocket):
        """Main handler for Twilio WebSocket connection"""
        await twilio_ws.accept()

        async with await self.connect_sts() as sts_ws:
            await self.send_sts_configuration(sts_ws)

            # Run concurrent tasks
            await asyncio.gather(
                self.sts_sender(sts_ws),
                self.sts_receiver(sts_ws, twilio_ws),
                self.twilio_receiver(twilio_ws)
            )

@app.websocket("/media-stream")
async def twilio_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for Twilio connections"""
    deepgram_api_key = "b749e403c7141d930ad7cd1e7b7f5d4a96e114b5"  # Replace with actual key
    handler = DeepgramSTSHandler(deepgram_api_key)
    await handler.handle_twilio_connection(websocket)

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()
    # <Say> punctuation to improve text-to-speech flow
    response.say(
        "Please wait while we connect your call to the A. I. voice assistant, powered by &.ai."
    )
    response.pause(length=1)
    response.say("O.K. you can start talking!")
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

# Run with: uvicorn main:app --host 0.0.0.0 --port 8000
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)