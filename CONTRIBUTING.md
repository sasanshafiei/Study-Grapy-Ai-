# Contributing

Five people in one semester. These rules keep us from stepping on each other.

## Branches & PRs

- `main` is protected. Nobody pushes to it directly.
- One branch per task: `ce1/document-upload`, `ai1/hybrid-retrieval`, etc.
- Keep PRs small (aim under ~400 lines) with **one reviewer**.
- A PR must pass CI (lint, type-check, tests, build) before merge.

## Ownership

You own your directories (see the table in the README). Don't merge into
someone else's directory without their review.

## The CE ↔ AI boundary

- `contracts/ai-service.openapi.yaml` is the source of truth for how the two
  teams talk. **Changing it needs sign-off from one CE and one AI member**, and
  a heads-up in the team channel before it merges.
- The CE side builds against the mock `ai-service`. The AI side replaces mock
  handlers one at a time, keeping the same shapes.

## Database

- All schema changes go through CE-1's migrations. Don't edit tables by hand.
- The AI service may read `chunks/topics/questions/attempts` and write only
  `chunks/chunk_topics/mastery_scores/recommendations/analytics_*`.

## Commits

Write a short imperative subject line: `Add document upload endpoint`.
Reference the task if you track them.
