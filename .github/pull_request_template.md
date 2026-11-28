## Summary

<!-- Describe what this PR changes and why. -->

## Checklist

- [ ] PR title follows Conventional Commits (`type(scope): summary`)
- [ ] `.env` updated (with `# <service> Configuration` section and `CHANGE_TO_<NAME>` secret placeholders, if applicable)
- [ ] `README.md` updated (`.env` example block mirrors `.env` + service section added/updated, if applicable)
- [ ] `apps/home-server-dashboard/html/index.html` updated (for services with a web UI, if applicable)
- [ ] `make check` passes locally (`lint`, `validate`, `test`)
- [ ] Full review (`Review gate` on current `HEAD`) and CI pipeline (`CI gate`) pass before merge
