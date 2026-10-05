"""Provider contracts are tested with fake responses; no paid API calls."""
import base64
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import wave

import httpx
from PIL import Image

from app.config import Settings
from app.media import _duration, build_subtitles, validate_assets
from app.providers import (
    DemoProvider, LiveProvider, ProviderError, _download_public_asset,
    _PinnedHTTPSConnection, create_provider,
)


class ProviderContracts(unittest.IsolatedAsyncioTestCase):
    def live(self):
        return LiveProvider(Settings(openai_api_key="fake-test-key"))

    async def test_offline_scene_plan_preserves_narration_and_duration(self):
        provider = DemoProvider()
        script = "One idea starts here. It becomes a clear story. Then the scene comes alive."
        scenes = await provider.plan_scenes(script, 20, "9:16", "Cinematic")
        self.assertEqual(" ".join(s["narration"] for s in scenes), script)
        self.assertAlmostEqual(sum(s["duration_seconds"] for s in scenes), 20)
        self.assertEqual(len({s["id"] for s in scenes}), len(scenes))
        with tempfile.TemporaryDirectory() as directory:
            visual = await provider.generate_visual(scenes[0], Path(directory)/"visual.png", "9:16")
            with Image.open(visual["path"]) as image:
                self.assertEqual(image.size, (720, 1280))
            self.assertEqual(visual["provider"], "offline-generated-card")

    async def test_offline_silence_is_not_reported_as_speech(self):
        with tempfile.TemporaryDirectory() as directory, patch("app.providers.shutil.which", return_value=None):
            voice = await DemoProvider().generate_voice("A demonstration narration.", Path(directory)/"voice.wav")
            self.assertFalse(voice["has_speech"])
            self.assertEqual(voice["provider"], "offline-silent-fallback")
            with wave.open(voice["path"], "rb") as audio:
                self.assertGreater(audio.getnframes(), 0)

    async def test_scene_planner_rejects_omitted_narration(self):
        provider = self.live()
        provider._structured = AsyncMock(return_value={"scenes": [{
            "id": "untrusted", "narration": "Missing words.", "visual_prompt": "A sunset.", "duration_seconds": 12,
        }]})
        with self.assertRaisesRegex(ProviderError, "changed or omitted"):
            await provider.plan_scenes("All of the original words.", 12, "16:9", "Cinematic")

    async def test_supervisor_function_call_is_strict_and_bounded(self):
        provider = self.live()
        provider._json_request = AsyncMock(return_value={"output": [{
            "type": "function_call", "name": "generate_voice", "arguments": '{"reason":"The script is ready."}',
        }]})
        decision = await provider.choose_action({"completed_stages": ["scenes"]}, ["generate_voice", "generate_visuals"])
        self.assertEqual(decision["tool"], "generate_voice")
        payload = provider._json_request.call_args.kwargs["payload"]
        self.assertEqual(payload["tool_choice"], "required")
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertTrue(all(tool["strict"] for tool in payload["tools"]))
        self.assertTrue(all(tool["parameters"]["additionalProperties"] is False for tool in payload["tools"]))

    async def test_supervisor_cannot_execute_unoffered_tool(self):
        provider = self.live()
        provider._json_request = AsyncMock(return_value={"output": [{
            "type": "function_call", "name": "delete_everything", "arguments": '{"reason":"Bad tool"}',
        }]})
        with self.assertRaisesRegex(ProviderError, "ineligible"):
            await provider.choose_action({}, ["generate_voice"])

    async def test_structured_response_rejects_refusals(self):
        provider = self.live()
        provider._json_request = AsyncMock(return_value={"status": "completed", "output": [{
            "content": [{"type": "refusal", "refusal": "Provider refusal"}],
        }]})
        with self.assertRaisesRegex(ProviderError, "declined"):
            await provider.generate_concept("A brief", "Cinematic")

    async def test_image_provider_rejects_corrupt_bytes(self):
        provider = self.live()
        provider._json_request = AsyncMock(return_value={"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/"scene.png"
            with self.assertRaisesRegex(ProviderError, "corrupt"):
                await provider.generate_visual({"visual_prompt": "A scene"}, destination, "16:9")
            self.assertFalse(destination.exists())

    async def test_tts_rejects_a_success_response_that_is_not_audio(self):
        provider = self.live()
        actual_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"oops": "not audio"}))
        with tempfile.TemporaryDirectory() as directory, patch("app.providers.httpx.AsyncClient", side_effect=lambda **kwargs: actual_client(transport=transport, **kwargs)):
            destination = Path(directory)/"voice.wav"
            with self.assertRaisesRegex(ProviderError, "empty audio|invalid WAV"):
                await provider.generate_voice("Hello", destination)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".wav.part").exists())

    async def test_mocked_live_http_endpoints_complete_planning_and_assets(self):
        provider = self.live()
        script = "A solar panel captures sunlight. It creates useful electricity."
        received = []
        image_bytes = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(image_bytes, format="PNG")
        audio_bytes = io.BytesIO()
        with wave.open(audio_bytes, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\x00\x00"*24000)

        def respond(request):
            body = json.loads(request.content)
            received.append((request.url.path, body))
            if request.url.path.endswith("/responses"):
                name = body["text"]["format"]["name"]
                payload = {
                    "video_concept": {"title": "Solar power", "concept": "Explain sunlight conversion", "audience": "Learners", "tone": "Clear"},
                    "video_script": {"script": script},
                    "video_scenes": {"scenes": [
                        {"id": "one", "narration": "A solar panel captures sunlight.", "visual_prompt": "A solar panel at sunrise.", "duration_seconds": 4},
                        {"id": "two", "narration": "It creates useful electricity.", "visual_prompt": "A house powered by solar.", "duration_seconds": 4},
                    ]},
                }[name]
                return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]}]})
            if request.url.path.endswith("/images/generations"):
                return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes.getvalue()).decode()}]})
            if request.url.path.endswith("/audio/speech"):
                return httpx.Response(200, content=audio_bytes.getvalue())
            raise AssertionError(f"Unexpected API endpoint {request.url.path}")

        actual_client = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as directory, patch("app.providers.httpx.AsyncClient", side_effect=lambda **kwargs: actual_client(transport=httpx.MockTransport(respond), **kwargs)):
            concept = await provider.generate_concept("Solar energy", "Cinematic")
            result_script = await provider.generate_script("Solar energy", concept, 8)
            scenes = await provider.plan_scenes(result_script, 8, "16:9", "Cinematic")
            visual = await provider.generate_visual(scenes[0], Path(directory)/"image.png", "16:9")
            voice = await provider.generate_voice(scenes[0]["narration"], Path(directory)/"voice.wav")
            self.assertEqual(voice["provider"], "openai/gpt-4o-mini-tts")
            self.assertTrue(voice["has_speech"])
            self.assertTrue(Path(visual["path"]).is_file())
            self.assertEqual(scenes[0]["id"], "scene-01")
        self.assertEqual(len(received), 5)
        self.assertTrue(all(body.get("store") is False for path, body in received if path.endswith("/responses")))

    async def test_malformed_response_output_has_actionable_error(self):
        provider = self.live()
        for output in [None, {}, ["unexpected"], [{"content": ["unexpected"]}]]:
            with self.subTest(output=output):
                provider._json_request = AsyncMock(return_value={"status": "completed", "output": output})
                with self.assertRaises(ProviderError):
                    await provider.generate_concept("A brief", "Cinematic")

    async def test_video_bridge_rejects_cross_origin_polling_without_leaking_key(self):
        provider = LiveProvider(Settings(openai_api_key="fake", video_api_url="https://provider.example/generate", video_api_key="test-video-secret"))
        provider._json_request = AsyncMock(return_value={"id": "123", "status": "queued", "status_url": "https://attacker.example/status"})
        with self.assertRaisesRegex(ProviderError, "configured provider origin"):
            await provider.generate_visual({"visual_prompt": "A video", "duration_seconds": 5}, Path("unused.mp4"), "16:9")
        self.assertEqual(provider._json_request.call_count, 1)

    async def test_video_bridge_handles_polling_then_bounded_download(self):
        provider = LiveProvider(Settings(openai_api_key="fake", video_api_url="https://provider.example/generate"))
        provider._json_request = AsyncMock(side_effect=[
            {"id": "123", "status": "queued"},
            {"id": "123", "status": "complete", "asset_url": "https://cdn.example/video.mp4"},
        ])
        def download(url, destination, max_bytes, timeout):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"\x00\x00\x00\x20ftypisom\x00\x00\x00\x00")
        with tempfile.TemporaryDirectory() as directory, patch("app.providers.asyncio.sleep", new=AsyncMock()), patch("app.providers._download_public_asset", side_effect=download):
            result = await provider.generate_visual({"visual_prompt": "A video", "duration_seconds": 5}, Path(directory)/"video.png", "16:9")
            self.assertEqual(result["kind"], "video")
            self.assertEqual(Path(result["path"]).suffix, ".mp4")
        self.assertEqual(provider._json_request.call_args.args, ("GET", "https://provider.example/generate/123"))

    async def test_rate_limit_has_clear_error_without_response_body(self):
        provider = self.live()
        actual_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(429, json={"error": "sensitive provider data"}))
        with patch("app.providers.httpx.AsyncClient", side_effect=lambda **kwargs: actual_client(transport=transport, **kwargs)):
            with self.assertRaisesRegex(ProviderError, "quota") as caught:
                await provider.generate_concept("A brief", "Cinematic")
        self.assertNotIn("sensitive", str(caught.exception))

    def test_no_key_or_unknown_mode_fails_before_live_requests(self):
        with self.assertRaisesRegex(ProviderError, "OPENAI_API_KEY"):
            create_provider("live", Settings())
        with self.assertRaises(ProviderError):
            create_provider("anything", Settings())


class MediaSafety(unittest.TestCase):
    def test_streaming_wav_unknown_length_does_not_imply_hours_of_audio(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\x00\x00"*24000)
        data = bytearray(buffer.getvalue())
        marker = data.index(b"data")
        data[marker+4:marker+8] = b"\xff\xff\xff\xff"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"streaming.wav"
            path.write_bytes(data)
            self.assertAlmostEqual(_duration(path, "ffmpeg"), 1.0, places=2)

    def test_private_ipv4_ipv6_and_mixed_dns_addresses_are_blocked(self):
        cases = [["127.0.0.1"], ["::1"], ["169.254.169.254"], ["8.8.8.8", "10.0.0.1"]]
        for addresses in cases:
            with self.subTest(addresses=addresses), patch("app.providers.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
            ]), patch("app.providers._PinnedHTTPSConnection") as connect:
                with self.assertRaisesRegex(ProviderError, "private or local"):
                    _download_public_asset("https://example.com/media.mp4", Path("unused.mp4"), 10000, 5)
                connect.assert_not_called()

    def test_media_download_rejects_non_https_or_embedded_credentials(self):
        for url in ["http://example.com/a", "file:///private/a", "https://user:pass@example.com/a", "https://example.com:8443/a"]:
            with self.subTest(url=url), patch("app.providers.socket.getaddrinfo") as resolve:
                with self.assertRaises(ProviderError):
                    _download_public_asset(url, Path("unused.mp4"), 10000, 5)
                resolve.assert_not_called()

    def test_download_pins_the_checked_address_and_verifies_original_hostname(self):
        connection = _PinnedHTTPSConnection("example.com", "8.8.8.8", 5)
        raw_socket = MagicMock()
        connection._context = MagicMock()
        with patch("app.providers.socket.create_connection", return_value=raw_socket) as create:
            connection.connect()
        create.assert_called_once_with(("8.8.8.8", 443), timeout=5)
        connection._context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.com")

    def test_asset_redirect_is_not_followed(self):
        response = MagicMock(status=302)
        connection = MagicMock()
        connection.getresponse.return_value = response
        with tempfile.TemporaryDirectory() as directory, patch("app.providers.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]), patch("app.providers._PinnedHTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(ProviderError, "redirects"):
                _download_public_asset("https://example.com/a.mp4", Path(directory)/"video.mp4", 10000, 5)
            self.assertEqual(connection.request.call_count, 1)

    def test_caption_cues_cover_each_scene_in_order(self):
        scenes = [{"id": "a", "narration": "One two three four five six seven eight nine ten.", "duration_seconds": 5},
                  {"id": "b", "narration": "A new scene.", "duration_seconds": 3}]
        with tempfile.TemporaryDirectory() as directory:
            text = build_subtitles(scenes, Path(directory)/"captions.srt").read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:04,000", text)
        self.assertIn("00:00:05,000 --> 00:00:08,000", text)

    def test_asset_validation_rejects_corrupt_images(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory)/"image.png"
            image.write_bytes(b"not an image")
            voice = Path(directory)/"voice.wav"
            with wave.open(str(voice), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(b"\x00\x00"*24000)
            with self.assertRaisesRegex(ProviderError, "corrupt"):
                validate_assets([{"id": "scene-1", "duration_seconds": 2}],
                                [{"kind": "image", "path": str(image)}], [{"path": str(voice)}])

    def test_remote_video_playlist_is_rejected_before_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)/"video.mp4"
            video.write_text('#EXTM3U\nfile:///private/secret.mp4', encoding="utf-8")
            voice = Path(directory)/"voice.wav"
            voice.write_bytes(b"nonempty")
            with patch("app.media._probe") as probe:
                with self.assertRaisesRegex(ProviderError, "not a playlist"):
                    validate_assets([{"id": "scene-1", "duration_seconds": 2}],
                                    [{"kind": "video", "path": str(video)}], [{"path": str(voice)}])
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
