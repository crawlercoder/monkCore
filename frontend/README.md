# AI Agent · Console

Three-page React SPA for onboarding orgs and monitoring repo ingestion against
the FastAPI backend in `../app`.

## Pages

1. **Org Setup** (`/orgs/new`) — name + GitHub/GitLab token → `POST /v1/org/create`.
2. **Repo Setup** (`/repos/new`) — repo URL + org picker → `POST /v1/repo/register`.
3. **Repo List** (`/repos`) — shows all repos for the selected org with
   live status (`PENDING` → `CLONING` → `READY`), auto-polls every 3 s,
   per-row **Process** button calls `POST /v1/repo/process/{repo_id}`.

## Stack

- Vite + React 18 + TypeScript
- React Router 6
- Native `fetch`, zero server-state libraries (useState / useEffect + a
  small polling loop keep things explicit and easy to audit)
- Dark/light themes via CSS variables + `prefers-color-scheme`

## Run locally

```bash
# 1. Start the backend (from the repo root)
uvicorn app.main:app --reload --port 8000

# 2. In another shell:
cd frontend
cp .env.example .env      # optional — edit VITE_API_BASE_URL if backend isn't on :8000
npm install
npm run dev               # http://localhost:9000
```

The backend's default `CORS_ORIGINS` allow-lists `http://localhost:9000` and
`http://localhost:3000`.

## Environment

| Variable            | Default                 | Notes                         |
| ------------------- | ----------------------- | ----------------------------- |
| `VITE_API_BASE_URL` | `http://localhost:8000` | No trailing slash; set for prod deploys |

## Notes

- Tokens are submitted with `type="password"`, `autoComplete="off"`,
  `spellCheck={false}`. They're sent once via POST and never echoed
  back by the backend; the UI never persists them in local state after
  a successful submit.
- The **Process** button runs the worker synchronously — fine for
  localhost, not for a prod ingress with a tight idle timeout. For
  production use the SQS/EventBridge path instead.
- `last_org_id` is stored in `localStorage` so Pages 2 and 3 default
  to the most recently created org after a refresh.
