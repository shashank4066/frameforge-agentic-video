# Resume description and demonstration

Use these bullets after you have run the project and can explain its code and behavior. They describe the implemented architecture without adding unmeasured performance or business outcomes.

## Agentic AI Video Generation Pipeline

**Python, FastAPI, OpenAI Responses API, SQLite, Redis, FFmpeg, Docker**

- Built a supervised video generation pipeline with specialized stages for concept development, scripting, scene planning, visual generation, narration, subtitles, validation, and MP4 composition.
- Integrated structured LLM outputs and constrained function calling with human approval checkpoints, bounded retries, automatic media repair, and persisted stage recovery.
- Developed a FastAPI service and review dashboard with durable SQLite jobs, worker leases, optional Redis notifications, artifact provenance, and stage observability.
- Added configurable live image and speech generation alongside an offline demo mode, with FFmpeg rendering and Docker packaging for reproducible demonstrations.

## A shorter version

**Agentic AI Video Generation Pipeline | Python, FastAPI, OpenAI, SQLite, FFmpeg, Docker**

Built a supervised pipeline that transforms creative briefs into MP4 videos through planning, media generation, review, and composition. Implemented durable job checkpoints, retries, worker leases, human approvals, and a dashboard for artifacts and stage events.

## Five-minute interview demonstration

1. Start the service and submit a 24-second demo job. Explain that demo assets are local illustrations and offline narration rather than model-generated content.
2. Show the concept, script, and timed scenes at plan review. Edit the narration and scene plan together, then approve the plan.
3. Show generated visuals and narration availability at media review. Approve the media, then inspect the captions and play the final MP4.
4. Open the event history and discuss stage timings, bounded retries, persisted outputs, and why approval gates are enforced by the executor.
5. Explain lease recovery and the separate Redis worker configuration. Point out that provider requests can repeat after a crash before their outputs are committed.

For a live demonstration, configure your own credentials, select models available to your account, and verify a small job in advance. Explain which stages use the Responses API, image generation, speech synthesis, and FFmpeg. Present successful live outputs as evidence only after actually running them.

## Claims to keep accurate

- The shipped artifact is a local portfolio application; it is not evidence of production traffic, enterprise deployment, or a multi-tenant product.
- Redis provides optional worker notifications. SQLite owns job state, so the project should not be described as a distributed exactly-once queue.
- The video adapter is a generic contract. Name a particular video vendor only after you integrate and verify that vendor.
- Demo illustrations and offline narration should not be called AI-generated video assets. Describe live output according to the providers actually used.
- Add quantified speed, cost, quality, or reliability improvements only after measuring them against a documented baseline.
- Do not claim technologies such as Kubernetes, vector search, model fine-tuning, or a particular agent framework unless you add and verify them in this project.
