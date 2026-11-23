/**
 * Live API client: talks to core-api over HTTP.
 *
 * This file is also the web team's proposal for the public API. Every path
 * below is listed in contracts/core-api.web.md so the backend can implement
 * them one at a time. Until an endpoint exists, calling it here fails with a
 * readable error and the screen shows it; run in mock mode to see the full UI.
 *
 * Conventions:
 *   - Base URL comes from NEXT_PUBLIC_API_URL (default http://localhost:8000).
 *   - All paths sit under /api/v1.
 *   - The backend speaks snake_case JSON. We convert both ways here, so the
 *     rest of the app only ever sees the camelCase shapes in types.ts.
 *   - Auth is a bearer token in the Authorization header.
 */

import { getSession, setSession } from "@/lib/session";
import {
  ApiError,
  type Api,
  type ChatMessage,
  type Session,
} from "./types";

const BASE = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
const V1 = `${BASE}/api/v1`;

// --- snake_case <-> camelCase -------------------------------------------------

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

const toCamelKey = (k: string) => k.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase());
const toSnakeKey = (k: string) => k.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);

function convertKeys(value: Json, convert: (k: string) => string): Json {
  if (Array.isArray(value)) return value.map((v) => convertKeys(v, convert));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [convert(k), convertKeys(v, convert)]),
    );
  }
  return value;
}

const fromServer = <T,>(body: unknown) => convertKeys(body as Json, toCamelKey) as T;
const toServer = (body: unknown) => JSON.stringify(convertKeys(body as Json, toSnakeKey));

// --- Requests ---------------------------------------------------------------

function authHeaders(): Record<string, string> {
  const token = getSession()?.token;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function failure(response: Response): Promise<ApiError> {
  let message = "";
  try {
    const body = (await response.json()) as { detail?: unknown; message?: unknown };
    if (typeof body.message === "string") message = body.message;
    else if (typeof body.detail === "string") message = body.detail;
  } catch {
    // Not JSON: fall back to the status-based message below.
  }
  if (response.status === 401) {
    setSession(null);
    return new ApiError(message || "Your session has ended. Sign in again.", 401);
  }
  if (response.status === 403) {
    return new ApiError(message || "Your account is not allowed to do that.", 403);
  }
  if (response.status === 404) {
    return new ApiError(
      message || "The server does not have this yet. The endpoint may not be built.",
      404,
    );
  }
  return new ApiError(
    message || `The server could not complete the request (status ${response.status}).`,
    response.status,
  );
}

async function send(path: string, init: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${V1}${path}`, {
      ...init,
      headers: { ...authHeaders(), ...init.headers },
    });
  } catch {
    throw new ApiError(
      `Could not reach the server at ${BASE}. Check that core-api is running.`,
    );
  }
  if (!response.ok) throw await failure(response);
  return response;
}

async function get<T>(path: string): Promise<T> {
  const response = await send(path, { method: "GET" });
  return fromServer<T>(await response.json());
}

async function write<T>(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<T> {
  const response = await send(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : toServer(body),
  });
  if (response.status === 204) return undefined as T;
  return fromServer<T>(await response.json());
}

// --- The API ----------------------------------------------------------------

export const liveApi: Api = {
  mode: "live",

  async login(email, password) {
    const body = await write<{ accessToken: string; user: Session["user"] }>(
      "POST",
      "/auth/login",
      { email, password },
    );
    return { token: body.accessToken, user: body.user };
  },

  listCourses: () => get("/courses"),
  listTopics: (courseId) => get(`/courses/${courseId}/topics`),

  getStudentDashboard: (courseId) => get(`/courses/${courseId}/dashboard`),

  /**
   * Streams an answer over server-sent events:
   *   event: token   data: {"text": "next few words"}
   *   event: done    data: {the full ChatMessage, snake_case}
   *   event: error   data: {"message": "..."}
   */
  async ask(courseId, question, scope, onText) {
    const response = await send(`/courses/${courseId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: toServer({ question, scope }),
    });
    if (!response.body) throw new ApiError("The server sent an empty answer.");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let text = "";
    let done: ChatMessage | null = null;

    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");

        let event = "message";
        const data: string[] = [];
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data.push(line.slice(5).trim());
        }
        if (data.length === 0) continue;
        const payload: unknown = JSON.parse(data.join("\n"));

        if (event === "token") {
          text += (payload as { text: string }).text;
          onText(text);
        } else if (event === "done") {
          done = fromServer<ChatMessage>(payload);
        } else if (event === "error") {
          throw new ApiError((payload as { message: string }).message);
        }
      }
    }

    if (!done) throw new ApiError("The answer stopped before it was finished. Ask again.");
    return done;
  },

  generateQuiz: (courseId, request) => write("POST", `/courses/${courseId}/quizzes`, request),
  submitAttempt: (courseId, input) => write("POST", `/courses/${courseId}/attempts`, input),

  listDocuments: (courseId) => get(`/courses/${courseId}/documents`),

  async uploadDocument(courseId, file) {
    const form = new FormData();
    form.append("file", file);
    // No Content-Type header: the browser sets the multipart boundary itself.
    const response = await send(`/courses/${courseId}/documents`, { method: "POST", body: form });
    return fromServer(await response.json());
  },

  deleteDocument: (documentId) => write("DELETE", `/documents/${documentId}`),

  getClassAnalytics: (courseId) => get(`/courses/${courseId}/analytics`),
  listExams: (courseId) => get(`/courses/${courseId}/exams`),
  getExam: (examId) => get(`/exams/${examId}`),
  createExam: (courseId, title, blueprint) =>
    write("POST", `/courses/${courseId}/exams`, { title, blueprint }),
  updateExamItem: (examId, itemId, patch) =>
    write("PATCH", `/exams/${examId}/items/${itemId}`, patch),
  regenerateExamItem: (examId, itemId) =>
    write("POST", `/exams/${examId}/items/${itemId}/regenerate`),
  approveExam: (examId) => write("POST", `/exams/${examId}/approve`),

  getSystemOverview: () => get("/admin/overview"),
  listUsers: () => get("/admin/users"),
  createUser: (input) => write("POST", "/admin/users", input),
  updateUser: (userId, patch) => write("PATCH", `/admin/users/${userId}`, patch),
  listSources: () => get("/admin/sources"),
  createSource: (input) => write("POST", "/admin/sources", input),
  updateSource: (sourceId, patch) => write("PATCH", `/admin/sources/${sourceId}`, patch),
  runSource: (sourceId) => write("POST", `/admin/sources/${sourceId}/run`),
  listJobs: () => get("/admin/jobs"),
  retryJob: (jobId) => write("POST", `/admin/jobs/${jobId}/retry`),
};
