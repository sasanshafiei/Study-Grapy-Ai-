# Development/demo seed (WP-2)

`python -m app.seed.admin demo` creates a minimal General Chemistry dataset for local
development and demos. It is for **development and test databases only**.

| Row | Identity (unique in the schema) | Created as |
| --- | --- | --- |
| Admin | `admin@demo.studygraph.test` | role `admin`, `active` |
| Professor | `professor@demo.studygraph.test` | role `professor`, `active` |
| Student | `student@demo.studygraph.test` | role `student`, `active` |
| Course | join code `SEEDCHEM` | "General Chemistry" (`CHEM DEMO`), owned by the professor |
| Enrollment | student × course | role `student` |
| Topics | via the [topic import](course-topics.md) | four placeholder topics, or `--topics FILE` |

The packaged topics (`services/core-api/app/seed/demo_topics.json`) are a development sample,
not a curriculum; AI-2 owns real topic maps.

## Usage

Run inside the core-api (or a worker) container whose mounted checkout contains this code.
`ENVIRONMENT` must be set explicitly in the command's environment.

```bash
# Preview. Writes nothing.
docker compose exec -e ENVIRONMENT=development core-api \
  python -m app.seed.admin demo --database studygraph --dry-run

# Create whatever is missing (safe to repeat)
docker compose exec -e ENVIRONMENT=development core-api \
  python -m app.seed.admin demo --database studygraph

# With your own topic file from the host
docker compose exec -T -e ENVIRONMENT=development core-api \
  python -m app.seed.admin demo --database studygraph --topics - < topics.json
```

Each output line is `created`/`exists`/`would create`, the kind, the id and a label, then a
topic summary. Exit status 1 with `error: …` means nothing was written.

## Guarantees

- **Refused** unless `ENVIRONMENT` is exactly `development` or `test` (the application's default
  does not count), the database target passes the operator-command checks (`DATABASE_URL`,
  migration safety rules, `--database` repeats its name), and the schema is at the revision this
  code requires. The topic file is validated before the database is contacted. The development
  Compose file already sets `ENVIRONMENT=development` in every container, so this guard is only
  as good as the deployment's own setting: staging and production must set another value.
- **Idempotent and additive.** Missing rows are created; existing rows are reused. A second run
  creates nothing.
- **Never modifies or deletes.** Display names, stored passwords, course name/code and existing
  topics are left as they are. If existing data contradicts the dataset (a demo e-mail with
  another role or a disabled account, the join code owned by another professor, the student
  enrolled with another role), the whole run is refused.
- **One transaction.** All rows are written or none are.
- **Concurrency.** Runs are serialized by a transaction-level advisory lock, and every lock wait
  is bounded (`lock_timeout` 10 s; a timeout writes nothing). Inserts use
  `ON CONFLICT DO NOTHING` with a re-read, so a concurrent writer of the same keys cannot cause
  duplicates or errors. Topics are imported under the course row lock that every topic writer
  must take.

## Limitations

- **No usable credentials.** CE-3 owns authentication and password hashing. Seeded users get
  `hashed_password = '!locked:studygraph-dev-seed'`, a marker that is not a hash of anything,
  so nobody can sign in as them until CE-3 gives them real credentials (see below). An existing
  user's stored value is never replaced, so credentials CE-3 sets later survive re-runs.
- The professor is the course owner and is not enrolled; the admin has no course relationship.
- No documents, questions, exams, attempts or harvest configuration are seeded.
- Nothing removes the seed. Delete demo rows by hand on a development database if needed.

## CE-3 integration requirement

The marker is public (it is in the repository), so it is safe only if CE-3's sign-in follows
these rules (the same convention as Django's unusable passwords):

1. A stored `hashed_password` that starts with `!` means "no password": refuse the sign-in
   *before* calling any hash verifier, with the ordinary invalid-credentials response (no 500).
2. Never compare a submitted password with the stored value directly.
3. Any other sign-in path (password reset, admin-set password, SSO, development tokens) must
   check account status and role on the server as usual; the seeded admin is a real `active`
   admin row once it can authenticate.
4. To make a demo account usable, set a real hash through CE-3's own flow. The seed never
   overwrites it.

A CE-3 test should assert that signing in as a seeded user with the marker text as the password
fails.
