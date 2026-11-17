# web (CE-2)

The StudyGraph web app: Next.js 16, React 19, TypeScript, Tailwind v4.

It covers all three roles — student, professor, admin — and runs **without a
backend** out of the box, so the rest of the team can see and use the whole
thing before core-api exists.

## Run it

```bash
npm install
npm run dev          # http://localhost:3000
```

Sign in with any of the demo accounts listed on the sign-in page (one click
each), or type the email with the password `studygraph-demo`:

| Email | Role |
| --- | --- |
| `student@studygraph.test` | Student |
| `professor@studygraph.test` | Professor |
| `admin@studygraph.test` | Admin |

Sample data lives in the browser's localStorage, so changes survive a reload.
**Reset sample data** at the bottom of the sidebar puts it back.

## Mock mode and live mode

```bash
# .env.local
NEXT_PUBLIC_API_MODE=mock            # default: sample data in the browser
# NEXT_PUBLIC_API_MODE=live          # real requests to core-api
# NEXT_PUBLIC_API_URL=http://localhost:8000
```

Every screen talks to one object, `api`, defined in `src/lib/api/`:

```
src/lib/api/
  types.ts      the Api interface and every shape the UI uses
  mock.ts       sample-data implementation (browser only)
  mock-data.ts  the sample course, question bank and canned answers
  live.ts       HTTP implementation, talks to core-api
  index.ts      picks one based on NEXT_PUBLIC_API_MODE
```

`live.ts` is also the web team's **request** to the backend: every path it
calls is listed in [`contracts/core-api.web.md`](../../contracts/core-api.web.md).
As CE-1 ships an endpoint, flip to live mode and that screen starts using it.
Anything not built yet fails with a readable message on screen rather than a
blank page.

The backend speaks `snake_case` JSON; `live.ts` converts both ways, so the rest
of the app only ever sees the camelCase types in `types.ts`.

## What is where

```
src/app/
  login/                  sign in
  student/                Today, Ask, Practice, Materials
  professor/              Class, Exams (list, blueprint, review), Materials
  admin/                  Overview, Users, Sources and jobs
  print/exams/[id]/       printable exam paper and answer key
src/components/
  shell/                  sidebar, theme toggle, wordmark
  ui/                     button, field, dialog, toast, panel, status
  charts/                 line, columns, meter list, all plain SVG
  answer.tsx              highlighted answer text and source cards
  materials.tsx           the materials screen, shared by two roles
src/lib/
  api/                    see above
  session.ts              who is signed in
  use-query.ts            load, reload, poll
  format.ts               dates, percentages, plurals
```

## Conventions worth keeping

**Colour carries meaning, never decoration.** Three highlighter colours say
where a sentence came from — class material (yellow), a fetched external source
(blue), a live web result (pink). They are the same three everywhere: behind
answer text, on source cards, in the materials list. Status colours
(good/warning/critical) always ship with an icon and a word, so nothing depends
on colour alone.

**Two typefaces, two jobs.** Atkinson Hyperlegible Next for the interface;
Literata (`.reading`) for study content — answers, question stems,
explanations. If a student reads it to learn, it goes in the reading face.

**Design tokens live in `globals.css`.** Both themes are defined there as CSS
variables. Never hard-code a colour in a component; add a token.

**Every screen handles four states**: loading, error with a retry, empty with a
next step, and content. `src/components/ui/feedback.tsx` has all three
non-content ones.

**Role checks here are for navigation only.** The server must enforce roles on
every request. Hiding a link is not security.

## Accessibility

Keyboard reachable throughout, visible focus rings, `prefers-reduced-motion`
respected, charts readable with arrow keys and switchable to a table view.
Worth keeping: it is also how the evaluation section of the report gets an
easy win.

## Checks

```bash
npx tsc --noEmit     # types
npx eslint src       # lint, including React's rules of hooks
npm run build        # production build
```
