/**
 * Mock API: the whole backend, faked in the browser.
 *
 * It keeps its state in localStorage so a demo survives a page reload, and it
 * behaves like the real thing where that matters to the UI: answers stream,
 * uploads and harvest jobs take a few seconds, mastery moves when a question
 * is answered. Swap it for the live client with NEXT_PUBLIC_API_MODE=live.
 */

import {
  ANSWERS,
  BANK,
  COURSE,
  HISTORY,
  SOURCES_REF,
  TOPICS,
  USAGE,
  cite,
  dayKey,
  daysAgo,
  seedDocuments,
  seedJobs,
  seedMastery,
  seedSources,
  seedUsers,
  topicName,
  type BankQuestion,
  type CannedAnswer,
  type SourceKey,
} from "./mock-data";
import {
  ApiError,
  type Api,
  type BlueprintRow,
  type ChatMessage,
  type Citation,
  type ClassAnalytics,
  type DocumentItem,
  type Exam,
  type ExamItem,
  type ExamSummary,
  type Job,
  type Question,
  type Recommendation,
  type Scope,
  type Source,
  type StudentDashboard,
  type TopicMastery,
  type User,
} from "./types";

export const DEMO_PASSWORD = "studygraph-demo";

const DB_KEY = "sg-mock-db-v2";

interface DayStat {
  correct: number;
  attempts: number;
  ms: number;
}

interface Db {
  users: User[];
  mastery: TopicMastery[];
  practice: Record<string, DayStat>;
  documents: DocumentItem[];
  exams: Exam[];
  sources: Source[];
  jobs: Job[];
  counter: number;
}

let db: Db | null = null;

function seed(): Db {
  return {
    users: seedUsers(),
    mastery: seedMastery(),
    practice: {},
    documents: seedDocuments(),
    exams: seedExams(),
    sources: seedSources(),
    jobs: seedJobs(),
    counter: 200,
  };
}

function state(): Db {
  if (db) return db;
  try {
    const raw = window.localStorage.getItem(DB_KEY);
    if (raw) {
      db = JSON.parse(raw) as Db;
      return db;
    }
  } catch {
    // Storage unavailable or corrupt: fall through to a fresh seed.
  }
  db = seed();
  return db;
}

function save() {
  try {
    window.localStorage.setItem(DB_KEY, JSON.stringify(state()));
  } catch {
    // Private mode or quota: the demo still works for this page load.
  }
}

/** Throw away everything changed during the demo and start from the seed. */
export function resetMockData() {
  try {
    window.localStorage.removeItem(DB_KEY);
  } catch {
    // Nothing stored, nothing to clear.
  }
  db = null;
}

function nextId(prefix: string): string {
  const s = state();
  s.counter += 1;
  return `${prefix}-${s.counter}`;
}

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const clamp = (n: number, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, n));

function shuffled<T>(items: T[]): T[] {
  const a = [...items];
  for (let i = a.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function bankQuestion(id: string): BankQuestion {
  const q = BANK.find((b) => b.id === id);
  if (!q) throw new ApiError("That question no longer exists.", 404);
  return q;
}

// --- Student ----------------------------------------------------------------

const TOPIC_MATERIAL: Record<string, { lecture: string; external: SourceKey }> = {
  "t-matter": { lecture: SOURCES_REF.l1SigFigs.title, external: "osMolarity" },
  "t-atomic": { lecture: SOURCES_REF.l2Radius.title, external: "osPolarity" },
  "t-bonding": { lecture: SOURCES_REF.l3Vsepr.title, external: "osPolarity" },
  "t-stoich": { lecture: SOURCES_REF.l4Limiting.title, external: "osYields" },
  "t-solutions": { lecture: SOURCES_REF.l5Molarity.title, external: "osMolarity" },
  "t-thermo": { lecture: SOURCES_REF.l6Sign.title, external: "osCalorimetry" },
  "t-equil": { lecture: SOURCES_REF.l7Principle.title, external: "osShifting" },
  "t-acids": { lecture: SOURCES_REF.l8Ph.title, external: "osPh" },
};

function percent(n: number): string {
  return `${Math.round(n * 100)}%`;
}

function buildRecommendations(mastery: TopicMastery[]): Recommendation[] {
  const weakestFirst = [...mastery].sort((a, b) => a.mastery - b.mastery);
  const [first, second] = weakestFirst;
  const recs: Recommendation[] = [];

  if (first) {
    const m = TOPIC_MATERIAL[first.topicId];
    const ext = SOURCES_REF[m.external];
    recs.push({
      id: `rec-quiz-${first.topicId}`,
      type: "practice_quiz",
      topicId: first.topicId,
      topicName: first.topicName,
      title: `Practise 5 questions on ${first.topicName}`,
      reason: `Your lowest topic: ${percent(first.mastery)} mastery after ${first.attempts} answers.`,
      origin: null,
      sourceTitle: null,
      url: null,
      minutes: 10,
    });
    recs.push({
      id: `rec-class-${first.topicId}`,
      type: "class_material",
      topicId: first.topicId,
      topicName: first.topicName,
      title: `Reread the ${first.topicName} lecture`,
      reason:
        first.trend === "down"
          ? "Your accuracy on this topic has fallen over the last week."
          : "The questions you missed all cite slides from this lecture.",
      origin: "class",
      sourceTitle: m.lecture,
      url: null,
      minutes: 20,
    });
    recs.push({
      id: `rec-ext-${first.topicId}`,
      type: "external_resource",
      topicId: first.topicId,
      topicName: first.topicName,
      title: ext.title,
      reason: `A second explanation of ${first.topicName}, with worked examples.`,
      origin: "external",
      sourceTitle: ext.title,
      url: ext.url,
      minutes: 15,
    });
  }

  if (second) {
    const ext = SOURCES_REF[TOPIC_MATERIAL[second.topicId].external];
    recs.push({
      id: `rec-ext-${second.topicId}`,
      type: "external_resource",
      topicId: second.topicId,
      topicName: second.topicName,
      title: ext.title,
      reason: `Second-lowest topic at ${percent(second.mastery)} mastery.`,
      origin: "external",
      sourceTitle: ext.title,
      url: ext.url,
      minutes: 15,
    });
  }

  const now = Date.now();
  const due = mastery.find(
    (t) =>
      t.mastery >= 0.7 &&
      t.nextReviewAt !== null &&
      new Date(t.nextReviewAt).getTime() <= now + 4 * 86_400_000,
  );
  if (due) {
    recs.push({
      id: `rec-review-${due.topicId}`,
      type: "review_topic",
      topicId: due.topicId,
      topicName: due.topicName,
      title: `Quick review of ${due.topicName}`,
      reason: `You know this at ${percent(due.mastery)}. A short review now keeps it from fading.`,
      origin: null,
      sourceTitle: null,
      url: null,
      minutes: 5,
    });
  }

  return recs;
}

function buildDashboard(): StudentDashboard {
  const s = state();
  const today = s.practice[dayKey(0)] ?? { correct: 0, attempts: 0, ms: 0 };

  // Days -13 .. -1 come from the seeded history; day 0 is whatever was
  // practised in this demo session.
  const days: { date: string; correct: number; attempts: number; minutes: number }[] = [];
  for (let back = 13; back >= 1; back -= 1) {
    const [correct, attempts, minutes] = HISTORY[HISTORY.length - back];
    const live = s.practice[dayKey(back)];
    days.push({
      date: dayKey(back),
      correct: correct + (live?.correct ?? 0),
      attempts: attempts + (live?.attempts ?? 0),
      minutes: minutes + Math.round((live?.ms ?? 0) / 60_000),
    });
  }
  days.push({
    date: dayKey(0),
    correct: today.correct,
    attempts: today.attempts,
    minutes: Math.round(today.ms / 60_000),
  });

  const week = days.slice(-7);
  const weekAttempts = week.reduce((n, d) => n + d.attempts, 0);
  const weekCorrect = week.reduce((n, d) => n + d.correct, 0);

  let streak = 0;
  for (let i = days.length - 1; i >= 0; i -= 1) {
    if (days[i].attempts > 0) streak += 1;
    else if (i === days.length - 1) continue; // today not practised yet
    else break;
  }

  return {
    mastery: s.mastery,
    recommendations: buildRecommendations(s.mastery),
    accuracyByDay: days.map((d) => ({
      date: d.date,
      accuracy: d.attempts > 0 ? d.correct / d.attempts : null,
      attempts: d.attempts,
    })),
    minutesByDay: days.map((d) => ({ date: d.date, minutes: d.minutes })),
    stats: {
      questionsAnswered: s.mastery.reduce((n, t) => n + t.attempts, 0),
      accuracy: weekAttempts > 0 ? weekCorrect / weekAttempts : 0,
      streakDays: streak,
      minutesThisWeek: week.reduce((n, d) => n + d.minutes, 0),
    },
  };
}

const SUBSCRIPTS: Record<string, string> = {
  "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
  "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
};

function normalise(text: string): string {
  return text
    .toLowerCase()
    .replace(/[₀-₉]/g, (c) => SUBSCRIPTS[c] ?? c)
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

function findAnswer(question: string): CannedAnswer | null {
  const q = ` ${normalise(question)} `;
  let best: CannedAnswer | null = null;
  let bestScore = 0;
  for (const answer of ANSWERS) {
    let score = 0;
    for (const keyword of answer.keywords) {
      const k = normalise(keyword);
      // Short keywords ("ph", "kc") must match a whole word.
      const hit = k.length <= 3 ? q.includes(` ${k} `) : q.includes(` ${k}`);
      if (hit) score += 1;
    }
    if (score > bestScore) {
      best = answer;
      bestScore = score;
    }
  }
  return best;
}

function composeAnswer(question: string, scope: Scope) {
  const match = findAnswer(question);
  if (!match) return null;

  const sentences: string[] = [];
  const citations: Citation[] = [];
  for (const part of match.parts) {
    const origin = SOURCES_REF[part.source].origin;
    let key: SourceKey | null = part.source;
    if (origin === "web" && scope !== "class_plus_web") key = null;
    if (origin === "external" && scope === "class_only") {
      key = part.classSource ?? null;
    }
    if (key === null) continue;

    let marker = citations.find((c) => c.chunkId === `chunk-${key}`)?.marker;
    if (marker === undefined) {
      marker = citations.length + 1;
      citations.push(cite(key, marker));
    }
    sentences.push(`${part.text} [${marker}]`);
  }
  if (sentences.length === 0) return null;
  return { text: sentences.join(" "), citations };
}

// --- Professor --------------------------------------------------------------

const CLASS_AVERAGES: Record<string, [number, number, number]> = {
  // topicId: [average mastery, attempts, students with data]
  "t-matter": [0.81, 512, 41],
  "t-atomic": [0.74, 468, 40],
  "t-bonding": [0.66, 431, 39],
  "t-stoich": [0.59, 590, 41],
  "t-solutions": [0.57, 344, 36],
  "t-thermo": [0.48, 362, 35],
  "t-equil": [0.39, 405, 33],
  "t-acids": [0.34, 188, 24],
};

function toExamItem(q: BankQuestion, points: number, status: ExamItem["status"]): ExamItem {
  return {
    id: `item-${q.id}`,
    topicId: q.topicId,
    topicName: topicName(q.topicId),
    type: q.type,
    difficulty: q.difficulty,
    stem: q.stem,
    choices: q.choices,
    answer: q.answer,
    rubric: q.rubric,
    points,
    status,
    flags: q.flags ?? [],
    citation: cite(q.source),
  };
}

function seedExams(): Exam[] {
  const pick = (ids: string[], points: number, status: ExamItem["status"]) =>
    ids.map((id) => toExamItem(BANK.find((b) => b.id === id)!, points, status));

  const midterm: Exam = {
    id: "exam-1",
    title: "Midterm 1: Units 1 to 4",
    status: "approved",
    createdAt: daysAgo(21),
    questionCount: 0,
    totalPoints: 0,
    approvedCount: 0,
    blueprint: [
      { topicId: "t-matter", type: "mcq", difficulty: "easy", count: 2, pointsEach: 5 },
      { topicId: "t-atomic", type: "mcq", difficulty: "medium", count: 2, pointsEach: 5 },
      { topicId: "t-bonding", type: "mcq", difficulty: "medium", count: 3, pointsEach: 5 },
      { topicId: "t-stoich", type: "mcq", difficulty: "medium", count: 3, pointsEach: 5 },
      { topicId: "t-stoich", type: "short", difficulty: "medium", count: 1, pointsEach: 10 },
    ],
    items: [
      ...pick(
        ["q-sigfig", "q-air", "q-radius", "q-valence", "q-nacl", "q-co2-shape", "q-polar",
          "q-moles-water", "q-co2-ratio", "q-limiting"],
        5,
        "approved",
      ),
      ...pick(["q-s-limiting"], 10, "approved"),
    ],
  };

  const quiz: Exam = {
    id: "exam-2",
    title: "Quiz 3: Equilibrium and Acids",
    status: "in_review",
    createdAt: daysAgo(1),
    questionCount: 0,
    totalPoints: 0,
    approvedCount: 0,
    blueprint: [
      { topicId: "t-equil", type: "mcq", difficulty: "medium", count: 3, pointsEach: 5 },
      { topicId: "t-equil", type: "short", difficulty: "medium", count: 1, pointsEach: 10 },
      { topicId: "t-acids", type: "mcq", difficulty: "medium", count: 2, pointsEach: 5 },
    ],
    items: [
      ...pick(["q-pressure"], 5, "approved"),
      ...pick(["q-temperature", "q-kc"], 5, "pending"),
      ...pick(["q-s-lechatelier"], 10, "pending"),
      ...pick(["q-poh", "q-titration"], 5, "pending"),
    ],
  };

  return [quiz, midterm].map(withTotals);
}

function withTotals(exam: Exam): Exam {
  const kept = exam.items.filter((i) => i.status !== "rejected");
  return {
    ...exam,
    questionCount: kept.length,
    totalPoints: kept.reduce((n, i) => n + i.points, 0),
    approvedCount: exam.items.filter((i) => i.status === "approved").length,
  };
}

function summary(exam: Exam): ExamSummary {
  const { id, title, status, createdAt, questionCount, totalPoints, approvedCount } =
    withTotals(exam);
  return { id, title, status, createdAt, questionCount, totalPoints, approvedCount };
}

function findExam(examId: string): Exam {
  const exam = state().exams.find((e) => e.id === examId);
  if (!exam) throw new ApiError("That exam no longer exists.", 404);
  return exam;
}

function pickForBlueprint(blueprint: BlueprintRow[]): ExamItem[] {
  const used = new Set<string>();
  const items: ExamItem[] = [];
  for (const row of blueprint) {
    const pool = BANK.filter((q) => q.topicId === row.topicId && q.type === row.type);
    // Prefer the requested difficulty, then fall back to the rest of the pool.
    const ordered = [
      ...shuffled(pool.filter((q) => q.difficulty === row.difficulty)),
      ...shuffled(pool.filter((q) => q.difficulty !== row.difficulty)),
    ].filter((q) => !used.has(q.id));
    for (const q of ordered.slice(0, row.count)) {
      used.add(q.id);
      items.push(toExamItem(q, row.pointsEach, "pending"));
    }
  }
  return items;
}

// --- Admin ------------------------------------------------------------------

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const DOMAIN = /^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)+$/;

function finishJobLater(jobId: string, sourceId: string | null) {
  window.setTimeout(() => {
    const s = state();
    const job = s.jobs.find((j) => j.id === jobId);
    if (!job) return;
    job.state = "succeeded";
    job.finishedAt = new Date().toISOString();
    job.error = null;
    job.fetched = 20 + Math.floor(Math.random() * 60);
    job.indexed = Math.floor(Math.random() * 9);
    job.skipped = job.fetched - job.indexed;
    const source = s.sources.find((x) => x.id === sourceId);
    if (source) {
      source.lastRun = "ok";
      source.lastRunAt = job.finishedAt;
      source.error = null;
      source.documents += job.indexed;
    }
    // A parse or index job that now succeeds makes its document usable.
    const doc = s.documents.find((d) => d.title === job.subject);
    if (doc && job.kind !== "harvest") {
      doc.status = "ready";
      doc.error = null;
      doc.pages = doc.pages ?? 12;
    }
    save();
  }, 3200);
}

// --- The API ----------------------------------------------------------------

export const mockApi: Api = {
  mode: "mock",

  async login(email, password) {
    await wait(450);
    const user = state().users.find(
      (u) => u.email.toLowerCase() === email.trim().toLowerCase(),
    );
    if (!user || password !== DEMO_PASSWORD) {
      throw new ApiError("That email and password do not match an account.", 401);
    }
    if (user.status === "disabled") {
      throw new ApiError("This account is disabled. Ask an admin to enable it.", 403);
    }
    user.lastActiveAt = new Date().toISOString();
    save();
    return { token: `mock.${user.id}`, user };
  },

  async listCourses() {
    await wait(120);
    return [COURSE];
  },

  async listTopics() {
    await wait(80);
    return TOPICS;
  },

  async getStudentDashboard() {
    await wait(320);
    return buildDashboard();
  },

  async ask(_courseId, question, scope, onText) {
    await wait(700);
    const composed = composeAnswer(question, scope);
    const text =
      composed?.text ??
      (scope === "class_plus_web"
        ? "Nothing in your class materials, the external sources or the web results matched this question closely enough to cite. Try rephrasing it, or ask about a topic from the lectures."
        : "Your class materials and the external sources for this course do not cover this, so there is nothing to cite. Try rephrasing the question, or turn on web search.");

    const words = text.split(" ");
    let soFar = "";
    for (const word of words) {
      soFar = soFar ? `${soFar} ${word}` : word;
      onText(soFar);
      await wait(22);
    }

    const citations = composed?.citations ?? [];
    return {
      id: nextId("msg"),
      role: "assistant",
      text,
      citations,
      grounded: composed !== null,
      usedWeb: citations.some((c) => c.origin === "web"),
      scope,
      createdAt: new Date().toISOString(),
    } satisfies ChatMessage;
  },

  async generateQuiz(_courseId, request) {
    await wait(900);
    const inTopics = BANK.filter(
      (q) =>
        q.type === "mcq" &&
        (request.topicIds.length === 0 || request.topicIds.includes(q.topicId)),
    );
    const preferred =
      request.difficulty === "mixed"
        ? inTopics
        : inTopics.filter((q) => q.difficulty === request.difficulty);
    const rest = inTopics.filter((q) => !preferred.includes(q));
    const chosen = [...shuffled(preferred), ...shuffled(rest)].slice(0, request.count);
    if (chosen.length === 0) {
      throw new ApiError("There are no questions for those topics yet.", 404);
    }
    return chosen.map(
      (q): Question => ({
        id: q.id,
        topicId: q.topicId,
        topicName: topicName(q.topicId),
        type: q.type,
        difficulty: q.difficulty,
        stem: q.stem,
        choices: q.choices,
        hint: q.hint,
      }),
    );
  },

  async submitAttempt(_courseId, input) {
    await wait(260);
    const s = state();
    const q = bankQuestion(input.questionId);
    const correct = input.answer === q.answer;
    const row = s.mastery.find((m) => m.topicId === q.topicId);
    if (!row) throw new ApiError("That topic is not part of this course.", 404);

    const before = row.mastery;
    if (correct) {
      const gain = 0.14 * Math.max(0.4, 1 - 0.3 * input.hintCount);
      row.mastery = clamp(before + gain * (1 - before), 0, 0.99);
    } else {
      row.mastery = clamp(before - 0.12 * before, 0.02, 1);
    }
    row.attempts += 1;
    row.trend = row.mastery > before ? "up" : "down";
    row.nextReviewAt = daysAgo(correct ? -Math.ceil(1 + row.mastery * 6) : -1);

    const key = dayKey(0);
    const day = s.practice[key] ?? { correct: 0, attempts: 0, ms: 0 };
    day.attempts += 1;
    day.correct += correct ? 1 : 0;
    day.ms += clamp(input.responseMs, 0, 10 * 60_000);
    s.practice[key] = day;
    save();

    return {
      correct,
      correctAnswer: q.answer,
      explanation: q.explanation,
      citation: cite(q.source),
      masteryBefore: before,
      mastery: { ...row },
    };
  },

  async listDocuments() {
    await wait(240);
    return [...state().documents].sort((a, b) => b.addedAt.localeCompare(a.addedAt));
  },

  async uploadDocument(_courseId, file) {
    await wait(500);
    const allowed = /\.(pdf|pptx|docx|txt|md)$/i;
    if (!allowed.test(file.name)) {
      throw new ApiError(
        `${file.name} is not a supported type. Upload a PDF, PPTX, DOCX, TXT or MD file.`,
        415,
      );
    }
    if (file.size > 25 * 1024 * 1024) {
      throw new ApiError(`${file.name} is larger than 25 MB.`, 413);
    }
    const s = state();
    const doc: DocumentItem = {
      id: nextId("doc"),
      title: file.name,
      origin: "class",
      kind: /syllabus/i.test(file.name) ? "syllabus" : /\.pptx?$/i.test(file.name) ? "slides" : "notes",
      status: "queued",
      pages: null,
      sourceName: null,
      url: null,
      licence: null,
      addedBy: "You",
      addedAt: new Date().toISOString(),
      error: null,
    };
    s.documents.push(doc);
    save();

    const id = doc.id;
    window.setTimeout(() => {
      const d = state().documents.find((x) => x.id === id);
      if (d && d.status === "queued") {
        d.status = "processing";
        save();
      }
    }, 1500);
    window.setTimeout(() => {
      const d = state().documents.find((x) => x.id === id);
      if (d && d.status === "processing") {
        d.status = "ready";
        d.pages = 8 + Math.floor(Math.random() * 30);
        save();
      }
    }, 5200);

    return doc;
  },

  async deleteDocument(documentId) {
    await wait(260);
    const s = state();
    s.documents = s.documents.filter((d) => d.id !== documentId);
    save();
  },

  async getClassAnalytics() {
    await wait(340);
    const topics = TOPICS.map((t) => {
      const [averageMastery, attempts, studentsWithData] = CLASS_AVERAGES[t.id];
      return { topicId: t.id, topicName: t.name, averageMastery, attempts, studentsWithData };
    });
    const totalAttempts = topics.reduce((n, t) => n + t.attempts, 0);
    const analytics: ClassAnalytics = {
      studentCount: COURSE.studentCount,
      activeThisWeek: 31,
      questionsAnswered: totalAttempts,
      classAccuracy: 0.61,
      hiddenForPrivacy: false,
      minimumGroupSize: 5,
      topics,
      hardestItems: BANK.filter((q) => q.type === "mcq")
        .sort((a, b) => a.correctRate - b.correctRate)
        .slice(0, 8)
        .map((q) => ({
          questionId: q.id,
          stem: q.stem,
          topicName: topicName(q.topicId),
          correctRate: q.correctRate,
          discrimination: q.discrimination,
          attempts: 30 + Math.round(q.discrimination * 20),
        })),
    };
    return analytics;
  },

  async listExams() {
    await wait(240);
    return state()
      .exams.map(summary)
      .sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  },

  async getExam(examId) {
    await wait(200);
    return withTotals(findExam(examId));
  },

  async createExam(_courseId, title, blueprint) {
    await wait(500);
    const s = state();
    const exam: Exam = withTotals({
      id: nextId("exam"),
      title: title.trim(),
      status: "generating",
      createdAt: new Date().toISOString(),
      questionCount: 0,
      totalPoints: 0,
      approvedCount: 0,
      blueprint,
      items: [],
    });
    s.exams.push(exam);
    save();

    const id = exam.id;
    window.setTimeout(() => {
      const e = state().exams.find((x) => x.id === id);
      if (!e) return;
      e.items = pickForBlueprint(e.blueprint);
      e.status = "in_review";
      save();
    }, 3000);

    return exam;
  },

  async updateExamItem(examId, itemId, patch) {
    await wait(180);
    const exam = findExam(examId);
    const item = exam.items.find((i) => i.id === itemId);
    if (!item) throw new ApiError("That question is no longer in this exam.", 404);
    Object.assign(item, patch);
    save();
    return withTotals(exam);
  },

  async regenerateExamItem(examId, itemId) {
    await wait(1100);
    const exam = findExam(examId);
    const index = exam.items.findIndex((i) => i.id === itemId);
    if (index === -1) throw new ApiError("That question is no longer in this exam.", 404);
    const current = exam.items[index];
    const inExam = new Set(exam.items.map((i) => i.id));
    const [replacement] = shuffled(
      BANK.filter(
        (q) =>
          q.topicId === current.topicId &&
          q.type === current.type &&
          !inExam.has(`item-${q.id}`),
      ),
    );
    if (!replacement) {
      throw new ApiError(
        `The course materials do not support another ${current.topicName} question of this type. Edit this one, or upload more material.`,
        409,
      );
    }
    exam.items[index] = toExamItem(replacement, current.points, "pending");
    save();
    return withTotals(exam);
  },

  async approveExam(examId) {
    await wait(350);
    const exam = findExam(examId);
    if (exam.items.some((i) => i.status === "pending")) {
      throw new ApiError("Approve or reject every question before approving the exam.", 409);
    }
    if (!exam.items.some((i) => i.status === "approved")) {
      throw new ApiError("An exam needs at least one approved question.", 409);
    }
    exam.status = "approved";
    save();
    return withTotals(exam);
  },

  async getSystemOverview() {
    await wait(300);
    const s = state();
    const running = s.jobs.filter((j) => j.state === "running").length;
    const failed = s.jobs.filter((j) => j.state === "failed").length;
    return {
      services: [
        { name: "Core API", state: "ok", detail: "Responding normally" },
        { name: "AI service", state: "ok", detail: "Responding normally" },
        { name: "Database", state: "ok", detail: "PostgreSQL with pgvector" },
        {
          name: "Job worker",
          state: failed > 0 ? "degraded" : "ok",
          detail:
            failed > 0
              ? `${failed} failed ${failed === 1 ? "job needs" : "jobs need"} attention`
              : running > 0
                ? `${running} ${running === 1 ? "job" : "jobs"} running`
                : "Idle",
        },
      ],
      counts: {
        users: s.users.length,
        courses: 1,
        documentsReady: s.documents.filter((d) => d.status === "ready").length +
          s.sources.reduce((n, x) => n + x.documents, 0),
        failedJobs: failed,
      },
      usageByDay: USAGE.map(([requests, tokens], i) => ({
        date: dayKey(USAGE.length - 1 - i),
        requests,
        tokens,
      })),
    };
  },

  async listUsers() {
    await wait(240);
    return [...state().users];
  },

  async createUser(input) {
    await wait(350);
    const s = state();
    const email = input.email.trim().toLowerCase();
    if (!input.displayName.trim()) throw new ApiError("Enter the person's name.", 422);
    if (!EMAIL.test(email)) throw new ApiError("Enter a full email address, like name@university.edu.", 422);
    if (s.users.some((u) => u.email.toLowerCase() === email)) {
      throw new ApiError("An account with that email already exists.", 409);
    }
    const user: User = {
      id: nextId("user"),
      displayName: input.displayName.trim(),
      email,
      role: input.role,
      status: "active",
      createdAt: new Date().toISOString(),
      lastActiveAt: null,
    };
    s.users.push(user);
    save();
    return user;
  },

  async updateUser(userId, patch) {
    await wait(220);
    const user = state().users.find((u) => u.id === userId);
    if (!user) throw new ApiError("That account no longer exists.", 404);
    Object.assign(user, patch);
    save();
    return user;
  },

  async listSources() {
    await wait(240);
    return [...state().sources];
  },

  async createSource(input) {
    await wait(350);
    const s = state();
    const domain = input.domain
      .trim()
      .toLowerCase()
      .replace(/^https?:\/\//, "")
      .replace(/\/.*$/, "");
    if (!input.name.trim()) throw new ApiError("Give the source a name.", 422);
    if (!DOMAIN.test(domain)) {
      throw new ApiError("Enter a domain like chem.libretexts.org, without a path.", 422);
    }
    if (s.sources.some((x) => x.domain === domain)) {
      throw new ApiError(`${domain} is already on the list.`, 409);
    }
    const source: Source = {
      id: nextId("src"),
      kind: "website",
      name: input.name.trim(),
      domain,
      licence: input.licence.trim() || "Not recorded",
      enabled: true,
      schedule: "Weekly, Wednesday 04:00",
      lastRunAt: null,
      lastRun: "never",
      documents: 0,
      error: null,
    };
    s.sources.push(source);
    save();
    return source;
  },

  async updateSource(sourceId, patch) {
    await wait(200);
    const source = state().sources.find((x) => x.id === sourceId);
    if (!source) throw new ApiError("That source no longer exists.", 404);
    Object.assign(source, patch);
    save();
    return source;
  },

  async runSource(sourceId) {
    await wait(300);
    const s = state();
    const source = s.sources.find((x) => x.id === sourceId);
    if (!source) throw new ApiError("That source no longer exists.", 404);
    if (!source.enabled) throw new ApiError("Turn the source on before running it.", 409);
    const job: Job = {
      id: nextId("job"),
      kind: "harvest",
      subject: source.name,
      state: "running",
      startedAt: new Date().toISOString(),
      finishedAt: null,
      fetched: 0,
      indexed: 0,
      skipped: 0,
      error: null,
    };
    s.jobs.push(job);
    source.lastRun = "running";
    save();
    finishJobLater(job.id, source.id);
    return job;
  },

  async listJobs() {
    await wait(240);
    return [...state().jobs].sort((a, b) => b.startedAt.localeCompare(a.startedAt));
  },

  async retryJob(jobId) {
    await wait(300);
    const s = state();
    const job = s.jobs.find((j) => j.id === jobId);
    if (!job) throw new ApiError("That job no longer exists.", 404);
    job.state = "running";
    job.startedAt = new Date().toISOString();
    job.finishedAt = null;
    job.error = null;
    const source = s.sources.find((x) => x.name === job.subject) ?? null;
    if (source) source.lastRun = "running";
    save();
    finishJobLater(job.id, source?.id ?? null);
    return job;
  },
};
