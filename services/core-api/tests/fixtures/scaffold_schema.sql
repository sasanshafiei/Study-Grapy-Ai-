CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';

CREATE TABLE public.attempts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    question_id uuid NOT NULL,
    topic_id uuid,
    is_correct boolean NOT NULL,
    hint_count integer NOT NULL,
    response_ms integer,
    attempted_at timestamp without time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.chunk_topics (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    chunk_id uuid NOT NULL,
    topic_id uuid NOT NULL
);

CREATE TABLE public.chunks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    course_id uuid,
    origin character varying(20) NOT NULL,
    text text NOT NULL,
    locator jsonb,
    embedding public.vector(384)
);

CREATE TABLE public.courses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    owner_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    join_code character varying(12) NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    course_id uuid,
    source_id uuid,
    origin character varying(20) NOT NULL,
    title character varying(255) NOT NULL,
    url text,
    licence character varying(100),
    language character varying(10) NOT NULL,
    content_hash character varying(64),
    status character varying(20) NOT NULL,
    fetched_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.enrollments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    course_id uuid NOT NULL,
    role character varying(20) NOT NULL
);

CREATE TABLE public.mastery_scores (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    topic_id uuid NOT NULL,
    mastery double precision NOT NULL,
    attempts integer NOT NULL,
    next_review_at timestamp without time zone,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.questions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    course_id uuid NOT NULL,
    topic_id uuid,
    purpose character varying(20) NOT NULL,
    qtype character varying(20) NOT NULL,
    difficulty character varying(20) NOT NULL,
    text text NOT NULL,
    choices jsonb,
    answer text,
    explanation text,
    citations jsonb,
    status character varying(20) NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.sources (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind character varying(30) NOT NULL,
    domain character varying(255),
    licence character varying(100),
    enabled boolean NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.topics (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    course_id uuid NOT NULL,
    name character varying(150) NOT NULL
);

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying(255) NOT NULL,
    display_name character varying(100) NOT NULL,
    hashed_password character varying(255) NOT NULL,
    role character varying(20) NOT NULL,
    status character varying(20) NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.attempts
    ADD CONSTRAINT attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.chunk_topics
    ADD CONSTRAINT chunk_topics_chunk_id_topic_id_key UNIQUE (chunk_id, topic_id);

ALTER TABLE ONLY public.chunk_topics
    ADD CONSTRAINT chunk_topics_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.courses
    ADD CONSTRAINT courses_join_code_key UNIQUE (join_code);

ALTER TABLE ONLY public.courses
    ADD CONSTRAINT courses_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT enrollments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT enrollments_user_id_course_id_key UNIQUE (user_id, course_id);

ALTER TABLE ONLY public.mastery_scores
    ADD CONSTRAINT mastery_scores_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mastery_scores
    ADD CONSTRAINT mastery_scores_user_id_topic_id_key UNIQUE (user_id, topic_id);

ALTER TABLE ONLY public.questions
    ADD CONSTRAINT questions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.topics
    ADD CONSTRAINT topics_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.attempts
    ADD CONSTRAINT attempts_question_id_fkey FOREIGN KEY (question_id) REFERENCES public.questions(id);

ALTER TABLE ONLY public.attempts
    ADD CONSTRAINT attempts_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id);

ALTER TABLE ONLY public.attempts
    ADD CONSTRAINT attempts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.chunk_topics
    ADD CONSTRAINT chunk_topics_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(id);

ALTER TABLE ONLY public.chunk_topics
    ADD CONSTRAINT chunk_topics_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id);

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.courses
    ADD CONSTRAINT courses_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id);

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT enrollments_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT enrollments_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.mastery_scores
    ADD CONSTRAINT mastery_scores_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id);

ALTER TABLE ONLY public.mastery_scores
    ADD CONSTRAINT mastery_scores_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.questions
    ADD CONSTRAINT questions_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);

ALTER TABLE ONLY public.questions
    ADD CONSTRAINT questions_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id);

ALTER TABLE ONLY public.topics
    ADD CONSTRAINT topics_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);
