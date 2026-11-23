import { liveApi } from "./live";
import { mockApi } from "./mock";
import type { Api } from "./types";

/**
 * The one API object every screen imports.
 *
 * NEXT_PUBLIC_API_MODE=mock  sample data in the browser, no backend needed (default)
 * NEXT_PUBLIC_API_MODE=live  real requests to core-api at NEXT_PUBLIC_API_URL
 */
export const api: Api = process.env.NEXT_PUBLIC_API_MODE === "live" ? liveApi : mockApi;

export * from "./types";
