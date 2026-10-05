import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_readiness_checks_access_once_and_never_exposes_key(tmp_path, monkeypatch):
    requests = []
    secret = "fake-readiness-secret"
    def respond(request):
        requests.append(request)
        assert request.headers["x-goog-api-key"] == secret
        assert request.url.params["pageSize"] == "1000"
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.1-flash-tts-preview", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/unrelated-private-model", "supportedGenerationMethods": ["generateContent"]},
        ]})
    original = httpx.AsyncClient
    monkeypatch.setattr("app.main.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    app = create_app(Settings(database_path=tmp_path / "jobs.db", storage_dir=tmp_path / "files", start_worker=False, gemini_api_key=secret))
    with TestClient(app) as client:
        for _ in range(2):
            response = client.get("/api/providers/gemini/status")
            data = response.json()
            assert data["connected"] and data["configured_text_available"] and data["configured_tts_available"]
            assert secret not in response.text and "unrelated-private-model" not in response.text
    assert len(requests) == 1
