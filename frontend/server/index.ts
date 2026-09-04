import express from "express";
import { createServer } from "http";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const fallbackIncidentResponse = {
  severity: "HIGH",
  steps: ["Evacuate", "Do not use water", "Move crosswind"],
  spoken_alert: "Evacuate now",
};

async function startServer() {
  const app = express();
  const server = createServer(app);

  app.use(express.json({ limit: "32kb" }));

  // Local/demo fallback. Replace this route with the real incident-response
  // service when the backend teammate/API is connected.
  app.post("/incident", (_req, res) => {
    res.json(fallbackIncidentResponse);
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
