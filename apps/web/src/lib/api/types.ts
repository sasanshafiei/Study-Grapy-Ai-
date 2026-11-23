/**
 * The shapes the web app works with.
 *
 * These are the web side of the public API contract with core-api. The mock
 * API (mock.ts) and the live client (live.ts) both implement the `Api`
 * interface at the bottom of this file, so a screen never knows which one it
 * is talking to. The backend sends snake_case JSON; live.ts converts it.
 */

export type Role = "student" | "professor" | "admin";

export interface User {
  id: string;
  email: string;
  displayName: string;
  role: Role;
  status: "active" | "disabled";
  createdAt: string;
  lastActiveAt: string | null;
}

export interface Session {
  token: string;
  user: User;
}

export interface Course {
  id: string;
  code: string;
  name: string;
  professorName: string;
  joinCode: string;
  studentCount: number;
}

export interface Topic {
  id: string;
  name: string;
}

// --- Sources and citations --------------------------------------------------

/** Where a piece of content came from. Drives the highlighter colours. */
export type Origin = "class" | "external" | "web";

/** What an answer is allowed to draw on. */
export type Scope = "class_only" | "class_plus_external" | "class_plus_web";

export interface Citation {
  marker: number;
  chunkId: string;
  origin: Origin;
  title: string;
  /** Human-readable position in the source: "Slide 12", "Section 4.4". */
  locator: string;
  url: string | null;
  snippet: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations: Citation[];
  /** False when the sources do not support an answer. */
  grounded: boolean;
  usedWeb: boolean;
  scope: Scope;
  createdAt: string;
}

export interface DocumentItem {
  id: string;
  title: string;
  origin: Origin;
  kind: "slides" | "notes" | "syllabus" | "textbook" | "article";
  status: "queued" | "processing" | "ready" | "failed";
  pages: number | null;
  sourceName: string | null;
  url: string | null;
  licence: string | null;
  addedBy: string | null;
  addedAt: string;
  error: string | null;
}

// --- Practice ---------------------------------------------------------------

export type QuestionType = "mcq" | "true_false" | "short";
export type Difficulty = "easy" | "medium" | "hard";

export interface Question {
  id: string;
  topicId: string;
  topicName: string;
  type: QuestionType;
  difficulty: Difficulty;
  stem: string;
  choices: string[];
  hint: string;
}

export interface QuizRequest {
  topicIds: string[];
  count: number;
  difficulty: Difficulty | "mixed";
}

export interface AttemptInput {
  questionId: string;
  answer: string;
  hintCount: number;
  responseMs: number;
}

export interface TopicMastery {
  topicId: string;
  topicName: string;
  /** 0 to 1, from the mastery model. */
  mastery: number;
  attempts: number;
  trend: "up" | "down" | "flat";
  nextReviewAt: string | null;
}

export interface AttemptResult {
  correct: boolean;
  correctAnswer: string;
  explanation: string;
  citation: Citation;
  masteryBefore: number;
  mastery: TopicMastery;
}

export type RecommendationType =
  | "review_topic"
  | "practice_quiz"
  | "class_material"
  | "external_resource";

export interface Recommendation {
  id: string;
  type: RecommendationType;
  topicId: string;
  topicName: string;
  title: string;
  /** Plain-language reason, shown to the student. */
  reason: string;
  origin: Origin | null;
  sourceTitle: string | null;
  url: string | null;
  minutes: number;
}

export interface DayAccuracy {
  date: string;
  /** 0 to 1, or null on a day with no answers. */
  accuracy: number | null;
  attempts: number;
}

export interface DayMinutes {
  date: string;
  minutes: number;
}

export interface StudentDashboard {
  mastery: TopicMastery[];
  recommendations: Recommendation[];
  accuracyByDay: DayAccuracy[];
  minutesByDay: DayMinutes[];
  stats: {
    questionsAnswered: number;
    accuracy: number;
    streakDays: number;
    minutesThisWeek: number;
  };
}

// --- Professor --------------------------------------------------------------

export interface TopicClassStat {
  topicId: string;
  topicName: string;
  averageMastery: number;
  attempts: number;
  studentsWithData: number;
}

export interface ItemStat {
  questionId: string;
  stem: string;
  topicName: string;
  /** Share of answers that were correct, 0 to 1. */
  correctRate: number;
  /** Point-biserial discrimination, -1 to 1. Higher separates strong from weak students better. */
  discrimination: number;
  attempts: number;
}

export interface ClassAnalytics {
  studentCount: number;
  activeThisWeek: number;
  questionsAnswered: number;
  classAccuracy: number;
  /** True when too few students have data to show averages without exposing individuals. */
  hiddenForPrivacy: boolean;
  minimumGroupSize: number;
  topics: TopicClassStat[];
  hardestItems: ItemStat[];
}

export interface BlueprintRow {
  topicId: string;
  type: QuestionType;
  difficulty: Difficulty;
  count: number;
  pointsEach: number;
}

export type ExamItemStatus = "pending" | "approved" | "rejected";
export type ExamFlag = "possible_duplicate" | "weak_source" | "ambiguous";

export interface ExamItem {
  id: string;
  topicId: string;
  topicName: string;
  type: QuestionType;
  difficulty: Difficulty;
  stem: string;
  choices: string[];
  /** The correct choice, "True"/"False", or a model answer for short questions. */
  answer: string;
  /** Marking guidance for short questions; empty otherwise. */
  rubric: string;
  points: number;
  status: ExamItemStatus;
  flags: ExamFlag[];
  citation: Citation;
}

export type ExamStatus = "generating" | "in_review" | "approved";

export interface ExamSummary {
  id: string;
  title: string;
  status: ExamStatus;
  createdAt: string;
  questionCount: number;
  totalPoints: number;
  approvedCount: number;
}

export interface Exam extends ExamSummary {
  blueprint: BlueprintRow[];
  items: ExamItem[];
}

export interface ExamItemPatch {
  status?: ExamItemStatus;
  stem?: string;
  answer?: string;
  rubric?: string;
  points?: number;
}

// --- Admin ------------------------------------------------------------------

export type HealthState = "ok" | "degraded" | "down";

export interface ServiceHealth {
  name: string;
  state: HealthState;
  detail: string;
}

export interface DayUsage {
  date: string;
  requests: number;
  tokens: number;
}

export interface SystemOverview {
  services: ServiceHealth[];
  counts: {
    users: number;
    courses: number;
    documentsReady: number;
    failedJobs: number;
  };
  usageByDay: DayUsage[];
}

export type SourceKind = "openstax" | "wikipedia" | "openalex" | "website";
export type RunState = "ok" | "failed" | "running" | "never";

export interface Source {
  id: string;
  kind: SourceKind;
  name: string;
  domain: string;
  licence: string;
  enabled: boolean;
  schedule: string;
  lastRunAt: string | null;
  lastRun: RunState;
  documents: number;
  error: string | null;
}

export type JobState = "queued" | "running" | "succeeded" | "failed";

export interface Job {
  id: string;
  kind: "harvest" | "parse" | "index";
  subject: string;
  state: JobState;
  startedAt: string;
  finishedAt: string | null;
  fetched: number;
  indexed: number;
  skipped: number;
  error: string | null;
}

export interface NewUser {
  displayName: string;
  email: string;
  role: Role;
}

export interface NewSource {
  name: string;
  domain: string;
  licence: string;
}

// --- The API ----------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number = 0,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface Api {
  /** "mock" serves sample data in the browser; "live" calls core-api. */
  readonly mode: "mock" | "live";

  login(email: string, password: string): Promise<Session>;
  listCourses(): Promise<Course[]>;
  listTopics(courseId: string): Promise<Topic[]>;

  // Student
  getStudentDashboard(courseId: string): Promise<StudentDashboard>;
  ask(
    courseId: string,
    question: string,
    scope: Scope,
    onText: (textSoFar: string) => void,
  ): Promise<ChatMessage>;
  generateQuiz(courseId: string, request: QuizRequest): Promise<Question[]>;
  submitAttempt(courseId: string, input: AttemptInput): Promise<AttemptResult>;

  // Materials (students and professors)
  listDocuments(courseId: string): Promise<DocumentItem[]>;
  uploadDocument(courseId: string, file: File): Promise<DocumentItem>;
  deleteDocument(documentId: string): Promise<void>;

  // Professor
  getClassAnalytics(courseId: string): Promise<ClassAnalytics>;
  listExams(courseId: string): Promise<ExamSummary[]>;
  getExam(examId: string): Promise<Exam>;
  createExam(
    courseId: string,
    title: string,
    blueprint: BlueprintRow[],
  ): Promise<Exam>;
  updateExamItem(
    examId: string,
    itemId: string,
    patch: ExamItemPatch,
  ): Promise<Exam>;
  regenerateExamItem(examId: string, itemId: string): Promise<Exam>;
  approveExam(examId: string): Promise<Exam>;

  // Admin
  getSystemOverview(): Promise<SystemOverview>;
  listUsers(): Promise<User[]>;
  createUser(input: NewUser): Promise<User>;
  updateUser(
    userId: string,
    patch: Partial<Pick<User, "role" | "status">>,
  ): Promise<User>;
  listSources(): Promise<Source[]>;
  createSource(input: NewSource): Promise<Source>;
  updateSource(
    sourceId: string,
    patch: Partial<Pick<Source, "enabled">>,
  ): Promise<Source>;
  runSource(sourceId: string): Promise<Job>;
  listJobs(): Promise<Job[]>;
  retryJob(jobId: string): Promise<Job>;
}
