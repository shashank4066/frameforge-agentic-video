import base64
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import wave

import httpx
import pytest

from app.gemini import GeminiProvider, _TtsPacer, _tts_pacer
from app.providers import FreeProvider, ProviderError


def settings(**overrides):
    values = {"gemini_api_key": "fake-gemini-secret", "gemini_model": "gemini-2.5-flash-lite",
              "gemini_tts_model": "gemini-2.5-flash-preview-tts", "gemini_tts_voice": "Kore",
              "pexels_api_key": "", "provider_timeout_seconds": 20}
    values.update(overrides)
    return SimpleNamespace(**values)


def text_result(payload):
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(payload)}]}}]}


@pytest.mark.asyncio
async def test_structured_concept_uses_direct_rest_header_schema_and_no_paid_features():
    requests = []
    concept = {"title": "Coffee", "concept": "A quiet coffee ritual", "audience": "Coffee lovers", "tone": "Warm"}

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=text_result(concept))

    actual_client = httpx.AsyncClient
    with patch("app.gemini.httpx.AsyncClient", side_effect=lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs)):
        result = await GeminiProvider(settings()).generate_concept("A cinematic coffee brand promo", "cinematic")
    assert result == concept
    request = requests[0]
    assert request.url.path.endswith("/gemini-2.5-flash-lite:generateContent")
    assert request.headers["x-goog-api-key"] == "fake-gemini-secret"
    assert "fake-gemini-secret" not in str(request.url)
    payload = json.loads(request.content)
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["responseJsonSchema"]["required"] == list(concept)
    assert "tools" not in payload and "cachedContent" not in payload


@pytest.mark.asyncio
async def test_gemini_scene_plan_preserves_words_and_normalizes_timing():
    provider = GeminiProvider(settings())
    provider._structured = AsyncMock(return_value={"scenes": [
        {"id": "untrusted-1", "narration": "Morning coffee.", "visual_prompt": "coffee cup; warm light", "duration_seconds": 2},
        {"id": "untrusted-2", "narration": "A quiet moment.", "visual_prompt": "cafe table; natural light", "duration_seconds": 3},
    ]})
    scenes = await provider.plan_scenes("Morning coffee. A quiet moment.", 15, "16:9", "cinematic")
    assert [scene["id"] for scene in scenes] == ["scene-01", "scene-02"]
    assert sum(scene["duration_seconds"] for scene in scenes) == pytest.approx(15)
    provider._structured.return_value["scenes"][1]["narration"] = "Words omitted."
    with pytest.raises(ProviderError, match="changed or omitted"):
        await provider.plan_scenes("Morning coffee. A quiet moment.", 15, "16:9", "cinematic")


@pytest.mark.asyncio
async def test_script_rejects_excess_words_instead_of_speeding_speech():
    provider = GeminiProvider(settings())
    provider._structured = AsyncMock(return_value={"script": "word " * 80})
    with pytest.raises(ProviderError, match="too much narration"):
        await provider.generate_script("A coffee promo", {}, 12)


@pytest.mark.asyncio
async def test_pcm_narration_gets_a_valid_24khz_mono_wav(tmp_path):
    provider = GeminiProvider(settings())
    pcm = b"\x01\x00" * 24000
    provider._request = AsyncMock(return_value={"candidates": [{"finishReason": "STOP", "content": {"parts": [{
        "inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()}
    }]}}]})
    result = await provider.generate_voice("A quiet coffee moment.", tmp_path / "voice.wav")
    with wave.open(result["path"], "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 24000
        assert audio.getnframes() == 24000
    payload = provider._request.call_args.args[1]
    assert payload["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert payload["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert result["has_speech"] is True and result["provider"].startswith("gemini/")
    assert "fake-gemini-secret" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mime, encoded", [("audio/L16;rate=44100", "AQABAA=="),
                                         ("audio/L16;rate=24000", "AQ=="),
                                         ("audio/mp3", "AQABAA=="),
                                         ("audio/L16;rate=24000", "!bad-base64!")])
async def test_bad_audio_is_rejected_without_partial_files(tmp_path, mime, encoded):
    provider = GeminiProvider(settings())
    provider._request = AsyncMock(return_value={"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": mime, "data": encoded}}]}}]})
    with pytest.raises(ProviderError):
        await provider.generate_voice("Hello world.", tmp_path / "bad.wav")
    assert not (tmp_path / "bad.wav").exists()
    assert not (tmp_path / "bad.wav.part").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status, message", [(403, "API key"), (429, "quota"), (404, "unavailable"), (500, "HTTP 500")])
async def test_gemini_http_failures_are_actionable_without_secret_echo(status, message):
    actual_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"error": "fake-gemini-secret private data"}))
    with patch("app.gemini.httpx.AsyncClient", side_effect=lambda **kwargs: actual_client(transport=transport, **kwargs)):
        with pytest.raises(ProviderError, match=message) as error:
            await GeminiProvider(settings()).generate_concept("A coffee promo", "cinematic")
    assert "fake-gemini-secret" not in str(error.value)
    assert "private data" not in str(error.value)


@pytest.mark.parametrize("overrides", [{"gemini_model": "../../paid-model"},
                                        {"gemini_model": "gemini-pro"},
                                        {"gemini_tts_model": "gemini-2.5-pro-preview-tts"},
                                        {"gemini_tts_voice": "unsupported-voice"},
                                        {"gemini_api_key": ""}])
def test_only_supported_config_is_accepted_before_any_request(overrides):
    with pytest.raises(ProviderError):
        GeminiProvider(settings(**overrides))


@pytest.mark.asyncio
async def test_free_provider_delegates_to_gemini_and_never_falls_back_on_quota(tmp_path):
    provider = FreeProvider(settings())
    provider._gemini.generate_script = AsyncMock(return_value="A quiet coffee moment.")
    assert await provider.generate_script("A coffee promo", {}, 12) == "A quiet coffee moment."
    provider._gemini.generate_voice = AsyncMock(side_effect=ProviderError("Gemini free-tier quota reached."))
    with patch("app.providers._local_voice") as local:
        with pytest.raises(ProviderError, match="quota"):
            await provider.generate_voice("Hello world.", tmp_path / "voice.wav")
    local.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_or_blocked_planning_does_not_publish_content():
    provider = GeminiProvider(settings())
    for response in [{"promptFeedback": {"blockReason": "SAFETY"}},
                     {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{}"}]}}]},
                     {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}]:
        provider._request = AsyncMock(return_value=response)
        with pytest.raises(ProviderError):
            await provider.generate_concept("A coffee promo", "cinematic")


@pytest.mark.asyncio
async def test_tts_pacer_serializes_jobs_and_spreads_starts_without_real_waiting():
    now = [100.0]
    sleeps = []
    starts = []
    active = [0]

    async def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        await asyncio.sleep(0)

    pacer = _TtsPacer(clock=lambda: now[0], sleep=sleep)

    async def operation():
        active[0] += 1
        assert active[0] == 1
        starts.append(now[0])
        await asyncio.sleep(0)
        active[0] -= 1
        return "voice"

    results = await asyncio.gather(*(pacer.request(operation) for _ in range(4)))
    assert results == ["voice"] * 4
    assert starts == [100.0, 121.0, 142.0, 163.0]
    assert sleeps == [21.0, 21.0, 21.0]
    assert _tts_pacer() is _tts_pacer()


@pytest.mark.asyncio
async def test_pacer_keeps_delay_after_quota_failure_without_provider_fallback():
    now = [100.0]
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    pacer = _TtsPacer(clock=lambda: now[0], sleep=sleep)

    async def quota():
        raise ProviderError("Gemini free-tier quota reached.")

    with pytest.raises(ProviderError, match="quota"):
        await pacer.request(quota)
    assert await pacer.request(AsyncMock(return_value="next voice")) == "next voice"
    assert waits == [21.0]
