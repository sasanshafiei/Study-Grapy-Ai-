# Course topics: operator import (WP-1)

A course's topics are the labels that harvest queries, quiz generation, exam blueprints,
chunk topic tags and the dashboard refer to. Until the AI topic extractor
(`studygraph.topics/1`, AI-2, proposed) exists, an operator loads them from a JSON file.
`python -m app.topics.admin` is the only production writer of `topics`.

## File format

```json
{"topics": [{"name": "Stoichiometry"}, {"name": "Chemical equilibrium"}]}
```

- Exactly one top-level key, `topics`: a non-empty array of at most 500 objects, each with
  exactly one key, `name`. UTF-8, at most 1 MiB, no repeated keys, no NaN/Infinity.
- Names are normalized (Unicode NFC, whitespace runs collapsed to one space, trimmed) and must
  be 1–150 characters with no control, format or other invisible characters.
- Two names in one file that differ only in case or spacing are refused.

The file is validated completely before the database is contacted. AI-2 owns the content of
the course topic map; `services/core-api/tests/fixtures/topics.dev-sample.json` is a
four-topic **development sample**, not a curriculum.

## Usage

Run inside the core-api (or a worker) container. The target is taken from `DATABASE_URL` and
verified as for the other operator commands (`--database` must repeat its name).

The command runs the code that the container has: the development Compose services bind-mount
`services/core-api` from the checkout they were started from. A container whose checkout predates
this change has no `app.topics` (`ModuleNotFoundError`); update that checkout first. No restart is
needed, because each command starts a new process. With plain `docker exec`, use `-i` instead of
`-T` for stdin.

```bash
# Preview: what would be created, what already exists, what is kept. Writes nothing.
docker compose exec core-api python -m app.topics.admin import \
  --database studygraph --course COURSE_ID --file tests/fixtures/topics.dev-sample.json --dry-run

# Import a file from the host through stdin
docker compose exec -T core-api python -m app.topics.admin import \
  --database studygraph --course COURSE_ID --file - < topics.json
```

Output lines are tab-separated: `created`/`would create`, `exists` (with
`stored as '…'; not renamed` when the spelling differs), and `kept` for existing topics the
file does not list, followed by a one-line summary. Exit status 1 with `error: …` on stderr
means nothing was written.

## Guarantees

- **Additive and idempotent.** Only missing names are inserted. Re-importing the same file
  changes nothing. Existing topics are never renamed (approved harvest queries are bound to the
  exact stored text) and never deleted (questions, attempts, mastery and queries reference them).
- **Identity.** A name matches an existing topic of the same course when both, normalized, are
  equal ignoring case.
- **Course isolation.** Every read and write is restricted to `--course`; the course must exist.
- **Concurrency.** An import locks its course row (`FOR NO KEY UPDATE`, `lock_timeout` 10 s), so
  imports of one course are serialized and never insert a name twice. Other courses, uploads and
  enrollments are not blocked; a course join waits for the short import transaction. A dry run
  takes no lock.
- **No schema change.** There is no unique index on `topics(course_id, name)`: the CLI is the only
  writer, and an index could not express the case/whitespace identity above.
- **Rule for future topic writers** (an API, the AI topic extractor, a seed): lock the course row
  `FOR NO KEY UPDATE` in the same transaction before reading the course's topic names, and compare
  names with `app.topics.catalogue.topic_key`. If that cannot be guaranteed, add a migration-backed
  uniqueness rule instead.

After an import, harvest queries are still approved one by one
(`python -m app.harvest.admin propose-queries` / `approve-query`, see
[external-harvesting.md](external-harvesting.md)).
