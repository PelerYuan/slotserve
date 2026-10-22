# Features

New frontend code goes into a folder per feature, so parallel work does not keep editing the same shared files
(`api/client.ts`, `api/types.ts`, `i18n/en.ts`, `i18n/zh-CN.ts`):

```
src/features/<feature>/
  api.ts        requests and types of this feature (built on the helpers in api/client.ts)
  i18n.ts       its texts in both languages
  components/   its components
```

## Texts

`i18n.ts` exports both languages; the Chinese texts are typed by the English ones, so a missing key fails the
typecheck:

```ts
export const en = {
  quickbook: {
    title: 'Quick booking',
  },
}

export const zh: typeof en = {
  quickbook: {
    title: '快速预约',
  },
}
```

`src/i18n/index.ts` picks up every `features/*/i18n.ts` with `import.meta.glob` and merges it into the shared
texts; nothing has to be registered. Use the feature's name as the top-level key (`t('quickbook.title')`). A
feature may add keys to a shared group such as `errors` (for its API error codes); it should not change existing
texts (development builds warn when it does).

## Rules

- All requests go through `api/client.ts` (it adds the headers SlotServe expects).
- Old code moves into `features/` when it is changed anyway; there is no big-bang migration.
- No "... hook" anchor comments in shared files: if a feature needs one, the shared file is missing an extension
  point.
