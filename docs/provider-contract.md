# Providers, uploads, and media provenance

The UI starts in **Free studio** and also offers **Demo** and **Live**. Free studio combines reviewed stock footage or user uploads with local or optional Gemini narration. Demo needs no key and uses illustrated cards. Live uses configured paid-capable providers. The API retains `provider_mode="demo"` as its compatibility default; select `"free"` for the free studio. Free jobs always require plan and media approval.

Keys belong in the server environment or local `.env`, never in the browser or a committed file. `/api/config` reports readiness without returning keys. Provider request attempts can repeat after a crash or retry; the app does not inspect or control account billing.

## Free studio with no API key

Concept/script/scene planning uses editable topic templates. Narration tries Windows `System.Speech`, then `espeak`, then an explicitly marked silent WAV. Docker includes `espeak`. Without a Pexels key, the visual stage produces draft cards marked for replacement. Users upload a real image/clip for every draft before media approval; the server blocks exporting those free-mode drafts.

`POST /api/samples/coffee` loads a prepared 16-second coffee job directly at media review. It imports existing AI-created images, saved Gemini 3.1 Flash TTS narration (Kore), and an original score. No new generation request is made. These curated sample assets do not provide automatic photorealistic generation for an arbitrary brief.

## Free Pexels stock footage

Set `PEXELS_API_KEY` to enable automatic video search in free mode. The visual adapter calls the Pexels video-search API with a short subject extracted from the scene prompt, the requested orientation, and a bounded result count. It chooses a direct MP4 with a suitable aspect ratio and practical dimensions, prefers different clip IDs across scenes, and downloads a local copy through the existing public-HTTPS media checks. Search failures, invalid credentials, and quota limits remain visible; no paid video provider is used as a fallback.

Each stock asset records its source URL, creator and creator URL, Pexels license link, query, source duration, and dimensions. The review/artifact data retains those credits. Downloaded footage is stock content rather than newly generated AI video. Check subject relevance and framing at media review and replace any poor match with your own clip. See the [Pexels API documentation](https://www.pexels.com/api/documentation/) and [Pexels License](https://www.pexels.com/license/).

## Optional Gemini scripts and neural speech

| Output | API integration | Default setting |
| --- | --- | --- |
| Concept, script, scene plan | Gemini `generateContent` with structured JSON | `GEMINI_MODEL=gemini-3.1-flash-lite` |
| Scene narration | Gemini `generateContent` with audio output | `GEMINI_TTS_MODEL=gemini-3.1-flash-tts-preview`, `GEMINI_TTS_VOICE=Kore` |

Create a key from a **free-tier** project in [Google AI Studio](https://aistudio.google.com/api-keys), set `GEMINI_API_KEY`, and restart the service. Google's [pricing reference](https://ai.google.dev/gemini-api/docs/pricing) lists free-tier text and speech access for these defaults, subject to quotas and account/model availability. Keep billing disabled on the project when you require no charges. A free-tier-eligible model can still incur charges if the key belongs to a paid project; this application cannot determine or change the project's billing status.

The Gemini adapter accepts only an allowlist of Flash-Lite text and Flash TTS models, uses standard requests, and sends no image, Veo, batch, or search-grounding calls. It does not request a paid OpenAI fallback. Errors for unavailable models, denied keys, blocked content, incomplete responses, and exhausted quotas are reported through normal bounded stage retries.

Gemini speech responses are decoded into a local PCM WAV after validating the audio response. Speech requests are serialized and paced on the worker loop to reduce quota bursts; this does not guarantee quota availability. Generated narration stays at its recorded rate. See [Gemini speech generation](https://ai.google.dev/gemini-api/docs/speech-generation) for supported prebuilt voices. Gemini supplies content and narration in free mode; workflow action selection remains deterministic.

## Media upload contract

While a job is `awaiting_review` at the `media` checkpoint, submit a multipart `POST /api/jobs/{id}/media` with:

| Field | Value |
| --- | --- |
| `file` | Binary image, video, or audio file |
| `kind` | `visual`, `voice`, or `music` |
| `scene_id` | Existing scene ID for a visual or voice; omit for music |

Limits and accepted sources:

| Asset | Formats | File limit | Duration limit |
| --- | --- | --- | --- |
| Scene visual | PNG, JPEG, WebP, MP4, WebM | 25 MB | 180 seconds for video |
| Scene narration | WAV, MP3, M4A, OGG | 15 MB | 60 seconds |
| Background music | WAV, MP3, M4A, OGG | 15 MB | 300 seconds |

The server inspects file content rather than trusting the filename, checks stream/duration/dimension limits, converts audio to local PCM WAV, and assigns generated filenames. Replacement invalidates downstream captions and export checkpoints. Uploaded narration should read the existing scene text; no automatic transcription updates captions. `POST /api/jobs/{id}/music/remove` clears music while awaiting media review.

At media approval, narration duration is measured and scene time is fitted without speech acceleration. The requested total is preserved when speech can fit with brief pauses; otherwise the total grows up to 60 seconds. Longer combined narration produces an error asking for shorter content. Captions are estimates distributed over actual recording duration, not word-aligned transcription. The composer loops music at a low level and ducks it beneath normalized narration; `transition="fade"` adds short fades to/from black, and `"cut"` retains hard cuts.

## Optional paid OpenAI live mode

| Pipeline output | Integration | Default setting |
| --- | --- | --- |
| Concept, script, scene plan | Responses API with structured outputs | `LLM_MODEL=gpt-4.1-mini` |
| Supervisor decisions | Responses API function calling | `LLM_MODEL=gpt-4.1-mini` |
| Scene images | Image generation API | `IMAGE_MODEL=gpt-image-1` |
| Narration | Speech API | `TTS_MODEL=gpt-4o-mini-tts`, `TTS_VOICE=alloy` |

Set `OPENAI_API_KEY` in the server environment and choose `provider_mode="live"`. The table reflects the existing adapter defaults; use model names currently available to your account. Availability, deprecations, and access are provider dependent. `OPENAI_BASE_URL` defaults to `https://api.openai.com/v1`. Live calls can incur charges, including repeated attempts. These integrations are separate from free studio.

Provider references: [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [image generation](https://developers.openai.com/api/docs/guides/image-generation), [text to speech](https://developers.openai.com/api/docs/guides/text-to-speech).

## Optional video endpoint

When `VIDEO_API_URL` is configured, live-mode visual generation uses an external text-to-video adapter instead of the OpenAI image endpoint. Free studio never calls this endpoint. This adapter is a documented integration contract, not a claim of a built-in integration with a named video vendor.

The adapter sends a JSON `POST` to `VIDEO_API_URL`:

```json
{
  "prompt": "A close-up of a leaf collecting rain, soft natural light",
  "duration_seconds": 5,
  "aspect_ratio": "16:9"
}
```

Set `VIDEO_API_KEY` when the service expects bearer authentication. The endpoint must return either an immediately downloadable asset:

```json
{"asset_url": "https://media.example.com/scene.mp4"}
```

or an asynchronous operation:

```json
{
  "id": "operation-123",
  "status": "queued",
  "status_url": "https://video.example.com/generate/operation-123"
}
```

The adapter polls the returned same-origin `status_url`, or `VIDEO_API_URL/{id}` when no status URL is supplied. A completed response supplies `asset_url` containing an MP4 or WebM file, not an HLS or DASH playlist. Download URLs must use public HTTPS addresses on port 443; the adapter pins the resolved public address while verifying TLS against the original hostname and restricts status polling to the configured provider origin. Serve the media directly without an interactive login page.

Statuses `failed`, `error`, `cancelled`, and `canceled` fail the attempt immediately. Other statuses are polled until an asset URL appears or the timeout expires. Media download redirects are declined. The timeout and failed response paths participate in bounded stage retries. If the optional video service still fails, the job fails with an event explaining the error; it does not silently switch to image generation. Remove `VIDEO_API_URL` and submit a new live job to use images instead.

## Demo media

Demo content uses deterministic topic templates and locally drawn scene cards. Narration uses Windows `System.Speech` when available, otherwise `espeak` when installed, otherwise a silent WAV explicitly flagged as containing no speech. Docker includes `espeak`. Demo assets demonstrate orchestration and rendering; they are not generated by an AI model. Demo jobs also accept asset replacements when reviews are enabled and receive the same natural-timing and composition improvements.

Local-system, Gemini, and OpenAI narration are synthetic speech. Preserve the recorded provenance when sharing outputs, and use footage, recordings, and music you have permission to publish.
