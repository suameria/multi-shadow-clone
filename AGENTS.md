# Repository working agreement

- Product, repository and planned Homebrew name: `multi-shadow-clone`.
- The owner currently develops directly on `develop`. Do not create development branches or Git worktrees for ordinary work. Commit completed, reviewed units frequently and push to `origin/develop` under the owner's standing authorization. Never commit unfinished changes, runtime data, credentials or research transcripts.
- Development remains paused until the owner explicitly resumes it. Repository saving, relocation and this initial private main/develop push are separately authorized.
- Use the existing Codex subscription only; no metered APIs, other inference providers, purchases, resets or silent model changes. Research/development subagent delegation is prohibited.
- Active implementation: `src/multi_shadow_clone`; Python uses the identifier `multi_shadow_clone`; product and CLI names use `multi-shadow-clone`. Domain has no I/O, Application owns transitions and ports, Infrastructure owns adapters, and bootstrap assembles them.
- Run `python3 -I tools/verification/host.py` for offline verification. Live product validation requires the owner's applicable authorization.
- Preserve failed and unknown receipts. Clean only owned resources. Never prune shared Docker/Git resources.
- The private checkpoint omits unfinished local work. Inspect the working-tree diff before staging and preserve those changes. Do not use `git add .` or `git add -A` without reviewing every included file.
