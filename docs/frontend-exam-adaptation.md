# Adapting the clients to the exam API (CE-2 web, CE-3 mobile)

Backend contract: [exam-api.md](exam-api.md) and [exam-export.md](exam-export.md). The backend is
authoritative; this note lists what the professor builder (CE-2) and the mobile approval screens (CE-3)
must do. No client code was changed in M9-A1. Until CE-3's authentication exists every call is 401; use
the backend test harness for contract tests.

## Requests

1. **Creates** (E2 question, E9 exam, E21 duplicate): send `Idempotency-Key` with a new UUID per logical
   create and **reuse it on retry**. 201 = created, 200 + `Idempotent-Replayed: true` = an earlier attempt
   already created it (the body is its current state), 409 `idempotency_key_reused` = the key was used for
   another body.
2. **Mutations**: send `If-Match: "<version>"` with the `version` from the **last JSON body** you received
   (do not rely on `ETag`: it is not readable cross-origin until CORS exposes it, decision D-25). Item and
   blueprint endpoints use the **exam** version. Handle 412 `version_mismatch` by re-reading and showing
   the change; 428 means the header is missing. A retried content change whose first attempt succeeded
   answers 412: re-read and compare.
3. **Exact bodies**: every listed key is required (send `null` for empty nullable fields); unknown keys are
   422 `invalid_fields`.
4. **True/false** questions: send `choices: null` (or the canonical array); responses always contain
   `[{"id":"true","text":"True"},{"id":"false","text":"False"}]`.
5. **Lists** are paginated with `limit` and an opaque `next_cursor` (no total count). Reuse a cursor only
   with the same filters and user; otherwise 422 `invalid_cursor` (start again from the first page).
6. Branch on `code` and the HTTP status; show `detail`. 503 `exam_busy` carries `Retry-After`.

## Builder screens (CE-2)

- **Bank**: list (E1, summaries without answers), detail with answer key (E3), edit (E4: when the
  response says `approval_revoked: true`, tell the professor the question must be approved again), approve
  (E5), retire (E6), revisions (E7). Show `source_refs[].available` (a document removed from the course
  blocks approval with `source_ref_unavailable`).
- **Blueprint** (E12): send the full row list in order; keep `id` for existing rows (also when only the
  order changes), `id: null` for new ones; rows not sent are deleted and their items become unassigned
  (`blueprint_change.unassigned_item_ids`). Show `assigned` / `count` per row.
- **Items**: add (E13, with an optional blueprint row whose criteria must match), points/row (E14), remove
  (E15), reorder (E16, the full permutation), fill (E17, shows `added`/`missing` per row; `stopped_by`
  appears when the export budget or the item limit stopped it).
- **Readiness**: show issues (blocking) and warnings (informational). Issues with `overridable: true`
  (blueprint deviations) can only be accepted with an explicit reason at finalization.
- **Review and finalize**: show the preview (E18) or a watermarked file (E23); keep the
  `preview_fingerprint` of **the preview the professor actually looked at**. Finalize (E19) with the same
  `If-Match` and that fingerprint:
  `{"preview_fingerprint": "pf1:…", "blueprint_deviation_reason": null}`. For an intentionally incomplete
  blueprint, a confirmation dialog lists the deviations and collects a 10–500 character reason.
  - 412 `exam_preview_stale`: something the professor reviewed changed (a question was re-approved, the
    course header changed, …). Show a fresh preview and ask for review again. **Never fetch a new
    fingerprint silently just to make the finalize pass.**
  - 412 `version_mismatch`: the exam itself changed; re-read.
  - 409 `exam_not_ready`: show `issues`; 422 `blueprint_override_not_needed`: drop the reason.
  - 409 `finalize_intent_mismatch`: the exam was already finalized with another preview or reason; re-read.
  - After a lost response, retry **with the identical body and header**: the server replays.
- **After finalization** the exam is immutable: archive (E20) or duplicate (E21; show `duplicate_report`
  and the `item_changed_since_source` warnings — the copy uses the current bank content).
- **Downloads** (E22): fetch with the bearer token as a blob and save it with the name from
  `Content-Disposition` (cross-origin this needs `Access-Control-Expose-Headers`, D-25). Never open answer
  keys in a shared tab or a third-party viewer.

## Confidentiality

Answer keys appear only in detail endpoints, previews with `variant=answer_key` and answer-key files.
All responses are `no-store`: do not cache them beyond the screen session; mobile must not persist them
to disk. Students get 403 `exams_not_allowed` on every exam endpoint.

## Mobile (CE-3)

Capstone p. 8: list and approve questions (E1, E3, E5). If mobile ever offers finalization it must send
the reviewed `preview_fingerprint` exactly as the web builder does. Exports are a web feature. AI candidate
review is M9-A2 and not available yet.

## Coordination still needed

- CE-3: authentication; CORS `expose_headers` (D-25); proxy timeouts ≥ 30 s for exports and body limit
  ≥ 64 KiB; image rebuild for the new Python dependencies; optional renderer hardening (D-30).
- CE-2: adopt the pagination convention (D-15/D-32) and the E19 body (D-38).
- AI-1: approve, change or reject the proposed `studygraph.exam/1` protocol (M9-A2); the shared OpenAPI
  file is not changed by M9-A1.
