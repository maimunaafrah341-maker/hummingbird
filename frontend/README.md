# Hazard Watch OS

Clean Manus-ready version of the single-page industrial incident console.

## Run locally

```bash
pnpm install
pnpm dev
```

Production build:

```bash
pnpm build
pnpm start
```

## Manus

Upload/import the project root into Manus. The project keeps the Manus Vite runtime and JSX-location plugins needed for the Manus editing environment, while unused starter UI components, login/auth scaffolding, storage/debug boilerplate, and analytics placeholders have been removed.

## Incident API

The included Express server exposes `POST /incident` as a safe demo fallback that returns the same response the original UI used when no teammate/backend endpoint was connected. Replace that route with the real incident-response service when available.

Camera and microphone controls require browser permission and a secure context (`https://` or localhost).
