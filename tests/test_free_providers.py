"""Free footage contracts use mocked HTTP; tests never spend provider credits."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.providers import FreeProvider, ProviderError, create_provider, _stock_query


def settings(key="free-test-secret"):
    return SimpleNamespace(pexels_api_key=key, provider_timeout_seconds=30)


def video(identifier=11, files=None):
    return {"id": identifier, "duration": 12,
            "url": f"https://www.pexels.com/video/coffee-{identifier}/",
            "user": {"name": "Test Filmmaker", "url": "https://www.pexels.com/@test/"},
            "video_files": files or [
                {"width": 1920, "height": 1080, "file_type": "video/mp4", "quality": "hd", "link": f"https://videos.pexels.com/{identifier}-1080.mp4"},
                {"width": 1280, "height": 720, "file_type": "video/mp4", "quality": "hd", "link": f"https://videos.pexels.com/{identifier}-720.mp4"},
            ]}


@pytest.mark.asyncio
async def test_free_planning_is_about_the_topic_and_keeps_narration():
    provider = create_provider("free", settings(""))
    concept = await provider.generate_concept("A cinematic coffee brand promo", "cinematic")
    script = await provider.generate_script("A cinematic coffee brand promo", concept, 30)
    assert "espresso" in script and "coffee" in script
    assert "voice, captions" not in script
    scenes = await provider.plan_scenes(script, 30, "16:9", "cinematic")
    assert " ".join(scene["narration"] for scene in scenes) == script
    assert sum(scene["duration_seconds"] for scene in scenes) == pytest.approx(30)
    assert "coffee" in scenes[0]["visual_prompt"]
    assert "espresso pouring" in scenes[2]["visual_prompt"]
    assert len({scene["visual_prompt"] for scene in scenes}) == len(scenes)


@pytest.mark.asyncio
async def test_no_key_creates_an_explicit_draft_without_network(tmp_path):
    provider = FreeProvider(settings(""))
    scene = {"id": "scene-01", "narration": "A warm cup of coffee.", "visual_prompt": "coffee cup"}
    with patch("app.providers.httpx.AsyncClient") as network:
        asset = await provider.generate_visual(scene, tmp_path / "draft.png", "16:9")
    network.assert_not_called()
    assert asset["draft"] is True
    assert asset["provider"] == "draft-placeholder"
    assert Path(asset["path"]).is_file()
    assert "Upload" in asset["placeholder_reason"]


@pytest.mark.asyncio
async def test_pexels_search_uses_official_path_raw_key_and_caches():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"videos": [video()]})

    actual_client = httpx.AsyncClient
    provider = FreeProvider(settings())
    with patch("app.providers.httpx.AsyncClient", side_effect=lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs)):
        first = await provider._search_videos("coffee beans", "9:16")
        second = await provider._search_videos("coffee beans", "9:16")
    assert first == second and len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/videos/search"
    assert request.headers["Authorization"] == "free-test-secret"
    assert request.url.params["query"] == "coffee beans"
    assert request.url.params["orientation"] == "portrait"
    assert request.url.params["size"] == "small"


@pytest.mark.asyncio
@pytest.mark.parametrize("status, message", [(401, "API key"), (403, "API key"), (429, "quota"), (500, "HTTP 500")])
async def test_search_failures_are_actionable_and_do_not_echo_response_secrets(status, message):
    actual_client = httpx.AsyncClient
    provider = FreeProvider(settings())
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"error": "free-test-secret sensitive-details"}))
    with patch("app.providers.httpx.AsyncClient", side_effect=lambda **kwargs: actual_client(transport=transport, **kwargs)):
        with pytest.raises(ProviderError, match=message) as error:
            await provider._search_videos("coffee", "16:9")
    assert "secret" not in str(error.value)
    assert "sensitive-details" not in str(error.value)


@pytest.mark.asyncio
async def test_footage_download_is_capped_and_credits_survive_resume(tmp_path):
    provider = FreeProvider(settings())
    provider._search_videos = AsyncMock(return_value=[video(11), video(12)])
    downloads = []

    def download(url, destination, limit, timeout):
        downloads.append((url, limit))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"0" * 50)

    scene = {"id": "scene-01", "visual_prompt": "coffee beans; cinematic footage"}
    with patch("app.providers._download_public_asset", side_effect=download):
        first = await provider.generate_visual(scene, tmp_path / "scene-01.png", "16:9")
        resumed = FreeProvider(settings())
        resumed._search_videos = AsyncMock(return_value=[video(11), video(12)])
        second = await resumed.generate_visual(scene, tmp_path / "scene-02.png", "16:9")
    assert first["source_id"] != second["source_id"]
    assert first["width"] == 1280 and first["height"] == 720
    assert all(limit == 30 * 1024 * 1024 for _, limit in downloads)
    assert downloads[0][0].endswith("720.mp4")
    assert first["creator"] == "Test Filmmaker"
    assert first["license_url"] == "https://www.pexels.com/license/"
    assert first["source_url"].startswith("https://www.pexels.com/video/")
    assert first["draft"] is False
    saved = json.loads(Path(first["path"]).with_suffix(".stock.json").read_text())
    assert saved["source_id"] == first["source_id"]
    assert "free-test-secret" not in json.dumps(saved)


def test_candidate_selection_skips_playlists_huge_files_and_bad_aspect():
    provider = FreeProvider(settings())
    files = [
        {"width": 1280, "height": 720, "file_type": "video/mp4", "quality": "hls", "link": "https://videos.pexels.com/a.m3u8"},
        {"width": 3840, "height": 2160, "file_type": "video/mp4", "link": "https://videos.pexels.com/b.mp4"},
        {"width": 720, "height": 1280, "file_type": "video/mp4", "link": "https://videos.pexels.com/c.mp4"},
    ]
    assert not provider._candidates([video(files=files)], "16:9")
    assert len(provider._candidates([video(files=files)], "9:16")) == 1


@pytest.mark.asyncio
async def test_no_result_or_corrupt_clip_fails_without_a_demo_fallback(tmp_path):
    provider = FreeProvider(settings())
    scene = {"id": "scene-01", "visual_prompt": "coffee beans; cinematic footage"}
    provider._search_videos = AsyncMock(return_value=[])
    with pytest.raises(ProviderError, match="No suitable"):
        await provider.generate_visual(scene, tmp_path / "missing.png", "16:9")
    provider._search_videos = AsyncMock(return_value=[video()])
    with patch("app.providers._download_public_asset", side_effect=lambda url, dest, limit, timeout: dest.write_text("not a video")):
        with pytest.raises(ProviderError, match="invalid MP4"):
            await provider.generate_visual(scene, tmp_path / "corrupt.png", "16:9")
    assert not (tmp_path / "corrupt.mp4").exists()


def test_search_keywords_are_short_and_remove_style_boilerplate():
    assert _stock_query("coffee beans grinding; cinematic natural footage") == "coffee beans grinding"
    assert _stock_query("Cinematic close shot of espresso pouring with warm lighting") == "espresso pouring"
    assert len(_stock_query("word " * 1000)) <= 90
