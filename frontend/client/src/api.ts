/**
 * Where the incident backend lives.
 *
 * One place, so pointing this console at the deployed backend is a
 * single environment variable and not a code change hunted through
 * components.
 *
 * VITE_API_BASE_URL is read at BUILD time, not at runtime -- Vite
 * inlines `import.meta.env.*` into the bundle. Changing it means
 * rebuilding and redeploying the frontend; setting it on the server
 * after the fact does nothing. That is a Vite property, not a choice
 * made here, and it is the thing most likely to waste an hour on
 * deployment day.
 *
 * Unset, it stays empty and every call is a same-origin relative path,
 * which is what local development wants.
 */

export const API_BASE_URL: string = (
  import.meta.env.VITE_API_BASE_URL ?? ""
).replace(/\/+$/, "");

/**
 * Absolute URL for an API path, or the relative path when no base is
 * configured.
 *
 *   apiUrl("/incident")  ->  "/incident"                          (dev)
 *   apiUrl("/incident")  ->  "https://api.example.com/incident"   (deployed)
 */
export function apiUrl(path: string): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE_URL}${suffix}`;
}

/** True when this build points at a backend on another origin. */
export const IS_REMOTE_API: boolean = API_BASE_URL.length > 0;
