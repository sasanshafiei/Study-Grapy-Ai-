# mobile (CE-3)

Expo (React Native) + TypeScript app, current Expo SDK.

Not scaffolded yet — this is CE-3's first task:

```bash
cd apps
npx create-expo-app@latest mobile
```

Carries all three roles at different depths (student = full; professor and
admin = lighter). Reuses the generated API client and data hooks from
`packages/`. Talks only to core-api.
