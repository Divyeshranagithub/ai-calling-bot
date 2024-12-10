import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv
import uvicorn

load_dotenv()

# Configuration
GROQ_API_KEY = "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"  # requires Groq API Access

SYSTEM_MESSAGE = (
    "You are a helpful and bubbly AI assistant who loves to chat about "
    "anything the user is interested in and is prepared to offer them facts. "
    "You have a penchant for dad jokes, owl jokes, and rickrolling – subtly. "
    "Always stay positive, but work in a joke when appropriate.")
VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'session.created'
]

app = FastAPI()
if not GROQ_API_KEY:
    raise ValueError(
        'Missing the Groq API key. Please set it in the .env file.')


@app.api_route("/", methods=["GET", "POST"])
async def index_page():
    return "<h1>Server is up and running. Groq-LLaMA integration active!</h1>"


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()
    response.say(
        "Please wait while we connect your call to the A. I. voice assistant, powered by Twilio and Groq AI."
    )
    response.pause(length=1)
    response.say("O.K. you can start talking!")
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and Groq API."""
    print("Client connected")
    await websocket.accept()

    async with websockets.connect(
            'wss://api.groq.com/v1/llama2-70b',
            extra_headers={
                "Authorization": f"Bearer {GROQ_API_KEY}"
            }) as groq_ws:
        await send_session_update(groq_ws)
        stream_sid = None

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the Groq API."""
            nonlocal stream_sid
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and groq_ws.open:
                        audio_append = {
                            "type": "input_audio",
                            "audio": data['media']['payload']
                        }
                        await groq_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
            except WebSocketDisconnect:
                print("Client disconnected.")
                if groq_ws.open:
                    await groq_ws.close()

        async def send_to_twilio():
            """Receive events from the Groq API, send audio back to Twilio."""
            nonlocal stream_sid
            try:
                async for groq_message in groq_ws:
                    response = json.loads(groq_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)
                    if response['type'] == 'session.updated':
                        print("Session updated successfully:", response)
                    if response[
                            'type'] == 'response.audio' and response.get(
                                'audio_delta'):
                        # Audio from Groq
                        try:
                            audio_payload = base64.b64encode(
                                base64.b64decode(
                                    response['audio_delta'])).decode('utf-8')
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": audio_payload
                                }
                            }
                            await websocket.send_json(audio_delta)
                        except Exception as e:
                            print(f"Error processing audio data: {e}")
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def send_session_update(groq_ws):
    """Send session update to Groq WebSocket."""
    session_update = {
        "type": "session.update",
        "session": {
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await groq_ws.send(json.dumps(session_update))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0")
