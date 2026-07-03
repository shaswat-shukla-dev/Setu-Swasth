// Vercel (frontend) + Render (backend) split deployment config.
//
// When the frontend is served from the same origin as the backend (e.g.
// running the FastAPI app locally, or via `docker compose up`), this file
// is not needed — the app auto-detects `location.origin`.
//
// When deploying the frontend separately on Vercel, set your Render
// backend's URL here so the frontend knows where to send API requests:
//
//   window.ANAMNIS_API_BASE = "https://setu-swasth.onrender.com";
//
// Leave it unset (or "") to auto-detect the current origin instead.
window.ANAMNIS_API_BASE = "";
