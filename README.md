# Bocconi AI Buddy - Starter

Starter project for the Bocconi Hackathon. Build an AI agent that helps Bocconi students across 4 areas of their university experience.

> **Read `BRIEF.md` first** for the high-level challenge description (what the AI Buddy is, the 4 verticals, evaluation flow). Then **read `AGENTS.md`** for the full technical specification, schema constraints, and rules.
> When you're ready to build the UI, read **`DESIGN.md`** for the editorial design system Codex should follow on the frontend.
> This README is only for setup and quick start.
> For deployment, read `DEPLOY.md` (or just ask Codex).
> For a feel of the questions the evaluator will ask, see `SAMPLE_QUESTIONS.md`.

## Prerequisites

Install on your machine before the event:

- **Docker Desktop** ([download](https://www.docker.com/products/docker-desktop)) - on Windows, enable WSL2
- **Codex Desktop App** ([install](https://developers.openai.com/codex/app)) or Codex CLI
- **Railway CLI** for deploy: `npm install -g @railway/cli` (or `brew install railway`)
- **Railway account** (free tier): https://railway.com

You'll get an **OpenAI redemption code** from the organizers (visible on the event platform). It is **not** an API key: redeem it on [platform.openai.com](https://platform.openai.com) to unlock the credits, then create your own API key from the dashboard. That generated key (`sk-...`) is what you paste into `.env`.

## Quick start (manual)

```bash
# 1. Set up your API key (the sk-... you generated after redeeming the code)
cp .env.example .env
# edit .env and paste your OPENAI_API_KEY

# 2. Start dev containers
docker compose -f docker-compose.dev.yml up -d

# 3. Verify
#    Backend at http://localhost:8000/docs
#    Frontend at http://localhost:5173
```

## Quick start (with Codex - recommended)

Open this folder in Codex Desktop App, select **Local** mode, and just ask:

> "Start the dev environment"

Codex runs `docker compose up -d` for you, waits for the containers to be healthy, and verifies the endpoints. From there, every command in this README can be triggered by just asking Codex.

## How to work with Codex on this project

This project is set up to be driven by Codex (Desktop App or CLI). The flow:

1. Open the folder in Codex Desktop App, select Local mode
2. Codex automatically reads `AGENTS.md` as project context
3. Use the **kickoff prompt** below for your first turn
4. Iterate: Codex edits files, you accept diffs, dev containers hot-reload

### Kickoff prompt (paste this as your first message to Codex)

```
Read AGENTS.md fully and confirm the constraints (frozen /ask schema,
30s latency cap, 4 verticals, English code, mostly-English evaluation).

Then implement the POST /ask endpoint in backend/main.py:
- Build a RAG pipeline over data/relocation/, data/life_on_campus/,
  data/study_abroad/, data/career_readiness/ (mixed IT+EN content -
  use a multilingual embedding model like text-embedding-3-large for
  good cross-lingual retrieval)
- Pick one vector store from the 3 options in AGENTS.md (FAISS, Chroma,
  or SQLite+sqlite-vec) and uncomment its dependencies in
  backend/pyproject.toml
- Detect the verticale from the question
- Return {answer, sources, verticale} matching the frozen schema
- Cover all 4 verticals: relocation, life_on_campus, study_abroad,
  career_readiness

After backend works end-to-end on one verticale, extend to all 4.
Then build a minimal React UI in frontend/src/ that calls /ask and
displays the answer with sources.

Smoke-test:
  curl -X POST http://localhost:8000/ask \
    -H 'Content-Type: application/json' \
    -d '{"question":"What MSc Finance courses are available?"}'
```

## Common things to ask Codex

You don't need to remember docker / curl / railway commands. Just ask Codex:

- *"Start the dev environment"* - brings up the containers
- *"Show me the backend logs"* - tails Docker logs
- *"Stop the containers"* - shuts everything down
- *"Restart the dev environment"* - useful when hot reload misbehaves
- *"Test the /ask endpoint with a sample question"* - smoke test
- *"Build the production image"* - runs `docker build -f Dockerfile.prod`
- *"Run the production image locally"* - sanity check before deploying
- *"Deploy to Railway"* - see `DEPLOY.md`. The first time, Codex will guide you through `railway login` + `railway init`
- *"Show Railway logs"* - tail logs of the deployed service
- *"Get the public Railway URL"* - generate or fetch the deployment URL

You can also run any of these manually in a terminal - Codex just makes it easier.

## Project layout

```
.
├── BRIEF.md                   # Challenge brief - read this first
├── AGENTS.md                  # Full technical spec - read this second
├── DESIGN.md                  # UI design system - read before touching frontend/
├── README.md                  # This file
├── DEPLOY.md                  # Step-by-step Railway deploy (two services) + Codex prompt
├── SAMPLE_QUESTIONS.md        # 10 sample evaluation questions (representative of the 80 used at scoring)
├── docker-compose.dev.yml     # Dev environment (backend + frontend, hot reload)
├── Dockerfile.backend         # Dev Dockerfile for backend
├── Dockerfile.frontend        # Dev Dockerfile for frontend
├── .env.example               # Template for OPENAI_API_KEY
├── .gitignore
├── backend/                   # Backend service (Python FastAPI)
│   ├── Dockerfile             # Production Dockerfile (deployed by Railway)
│   ├── railway.json           # Railway deploy config for the backend service
│   ├── pyproject.toml         # Essentials installed; RAG libs commented
│   ├── main.py                # FastAPI app + /ask endpoint (returns 501 until you implement)
│   └── data/                  # Pre-cleaned RAG knowledge base (bundled with backend)
│       ├── README.md          # Describes the dataset structure
│       ├── manifest.json      # Index of all data files with metadata
│       ├── extra-sources.md   # Curated additional public sources (some bundled, some pointers only)
│       ├── relocation/        # ~119 files - housing, visa, transport, banks, healthcare
│       ├── life_on_campus/    # ~473 files - associations, services, sport, dining, well-being
│       ├── study_abroad/      # ~132 files - exchange, partners, summer schools, Farnesina country advisories
│       └── career_readiness/  # ~893 files - programs, career, faculty, research, alumni, AlmaLaurea
└── frontend/                  # Frontend service (Vite + React)
    ├── railway.json           # Railway deploy config for the frontend service
    ├── package.json
    └── src/                   # Placeholder UI - you build this
```

Total dataset: **~1,617 files, ~2.88M tokens**, located at `backend/data/`.

## Two services on Railway

For the production deploy, your app runs as **two Railway services** in the same project:

- **backend** (Python FastAPI) - exposes `/ask` and `/health`. **This is the URL the evaluator hits.**
- **frontend** (Vite static site) - calls the backend via `VITE_BACKEND_URL`.

This makes the build robust (no fragile multi-stage), keeps the standard production split, and lets you redeploy each independently. See `DEPLOY.md` for step-by-step instructions, or just ask Codex to handle it.

## Constraints recap (from `AGENTS.md`)

- `/ask` request: `{"question": str}`. Response: `{"answer": str, "sources": list[str], "verticale": str}`. Schema is frozen.
- Response time: `≤ 30s` per request. Over = `wrong` (-15) for that question (system error counts as a wrong answer).
- All 4 verticals must be covered.
- Evaluation questions are mostly in English; knowledge base is mixed IT+EN. A multilingual embedding model is recommended for cross-lingual retrieval.
- Code, comments and identifiers in English. Sources must reference real paths in `data/`.
- Never commit `.env`. Never hardcode API keys.

## Submission

You'll deliver:

- The **backend public URL** (Railway). This is where the evaluator hits `/ask` with ~80 questions.
- The **frontend public URL** (Railway). What humans see during Level 2 evaluation.
- The full project zipped (code + .env.example - **no `.env` with the real key**)
- A short text description of your product (~200 words)

The backend URL is the most important: if `/ask` is not reachable, every question scores `wrong` (-15) and your Level 1 total goes to the floor (-1200). See `DEPLOY.md` for step-by-step Railway instructions, or just ask Codex to deploy it for you.

Details and submission form are on the event platform.

## Troubleshooting

- **Containers don't start**: check Docker Desktop is running, `docker ps` should work
- **Hot reload broken**: ask Codex to restart the dev environment
- **`/ask` returns 501**: that's expected - it means you haven't implemented the endpoint yet
- **OpenAI 401**: check `OPENAI_API_KEY` in `.env` and that the container picked it up. Ask Codex to verify with `docker compose exec backend env | grep OPENAI`
- **Deploy issue at the event**: a Yellow Tech mentor team is available in the room to help. **Do your first deploy around hour 3** (not at the last minute) so any issues surface while there is still time to fix them.
