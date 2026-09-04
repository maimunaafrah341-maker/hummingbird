import express from "express";
import { createServer } from "http";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// REMOVED: the local fallback incident response.
//
// It used to be:
//
//   const fallbackIncidentResponse = {
//     severity: "HIGH",
//     steps: ["Evacuate", "Do not use water", "Move crosswind"],
//     spoken_alert: "Evacuate now",
//   };
//
// and it answered every POST /incident with those three canned steps.
// That is indistinguishable from a working backend from the browser's
// side, which makes it possible to demo invented safety instructions
// believing they came from the real service. Deleted rather than left
// behind a flag, because a flag can be left on.
//
// Point the frontend at the real backend with VITE_API_BASE_URL --
// see .env.example.

async function startServer() {
  const app = express();
  const server = createServer(app);

  app.use(express.json({ limit: "32kb" }));

  // This server does NOT answer /incident. The route is kept only so the
  // failure is legible: without it, a POST would fall through to the
  // SPA catch-all below, which is a GET handler, and Express would
  // return a bare 404 that looks like a routing mistake rather than a
  // missing configuration.
  //
  // 501 Not Implemented is the honest status: the endpoint exists in the
  // contract, this process is simply not the thing that implements it.
  app.post("/incident", (_req, res) => {
    res.status(501).json({
      error: "not_implemented_here",
      detail:
        "This Express server does not implement /incident. Set " +
        "VITE_API_BASE_URL to the deployed backend and rebuild the " +
        "client, or run the backend on this origin. See .env.example.",
    });
  });

  const staticPath =
    process.env.NODE_ENV === "production"
      ? path.resolve(__dirname, "public")
      : path.resolve(__dirname, "..", "dist", "public");

  app.use(express.static(staticPath));

  app.get("*", (_req, res) => {
    res.sendFile(path.join(staticPath, "index.html"));
  });

  const port = Number(process.env.PORT) || 3000;
  server.listen(port, () => {
    console.log(`Hazard Watch OS running on http://localhost:${port}/`);
  });
}

startServer().catch((error) => {
  console.error("Failed to start Hazard Watch OS", error);
  process.exitCode = 1;
});
