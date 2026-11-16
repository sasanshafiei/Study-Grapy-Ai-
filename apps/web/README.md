# web (CE-2)

Next.js + TypeScript app for all three roles (student, professor, admin).

Not scaffolded yet — this is CE-2's first task. When you set it up:

```bash
cd apps
npx create-next-app@latest web --ts --app --eslint
```

Rules:
- Never hand-write fetch calls. Generate a typed client from the core API's
  OpenAPI file into `packages/api-client` and import it.
- Shared components and design tokens go in `packages/ui`.
- Talk only to core-api (http://localhost:8000). Never call ai-service directly.
