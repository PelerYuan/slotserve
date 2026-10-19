## Summary

<!-- What does this change and why? Link related issues: Fixes #123 -->

## Checklist

- [ ] `ruff check . && ruff format --check .` passes
- [ ] `cd backend && pytest` passes
- [ ] `cd frontend && npm run typecheck && npm run build` passes
- [ ] UI changes checked with `pytest e2e` (see `e2e/conftest.py`)
- [ ] New UI text added to both `en.ts` and `zh-CN.ts`
- [ ] Docs updated (English and Chinese) if behaviour or configuration changed
- [ ] `CHANGELOG.md` updated under **Unreleased**
