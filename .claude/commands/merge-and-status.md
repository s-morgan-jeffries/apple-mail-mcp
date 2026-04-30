Merge the current PR, pull main, surface any open contributor PRs and untriaged contributor issues, and show open milestone issues.

## Steps

1. **Find the open PR** for the current branch:
   ```bash
   gh pr list --head "$(git branch --show-current)" --state open --json number --jq '.[0].number'
   ```

2. **Wait for CI checks to pass:**
   ```bash
   gh pr checks <number> --watch
   ```

3. **Squash merge** the PR and delete the branch:
   ```bash
   gh pr merge <number> --squash --delete-branch
   ```

4. **Switch to main and pull:**
   ```bash
   git checkout main && git pull
   ```

5. **Surface open contributor PRs** (any PR the current GitHub user did not author). This is the catch-net for external contributions that might otherwise sit unreviewed for weeks:
   ```bash
   GIT_USER=$(gh api user --jq .login)
   gh pr list --state open --json number,title,author,createdAt \
     --jq "map(select(.author.login != \"$GIT_USER\")) | .[] | \"#\(.number)|\(.author.login)|\(.title)|\(.createdAt[:10])\""
   ```
   Behavioral rules for displaying the result:
   - Render as a table with columns: PR #, author, title, opened-on date.
   - If any rows appear, **call them out at the top of the final response** — e.g., "⚠️ N contributor PR(s) need attention." Don't bury this below the milestone-issues list.
   - If the same author appears multiple times, group them together in the table.
   - Dependabot PRs count as contributor PRs for visibility purposes (they need rebase nudges or merges too).
   - If zero rows, say "No open contributor PRs" so the user gets positive confirmation rather than ambiguity.

6. **Surface untriaged contributor issues** (open issues filed by anyone other than the current GitHub user that are NOT assigned to a milestone). The reasoning for the no-milestone filter: assigned issues have already been triaged and are visible elsewhere — the truly invisible ones are the unassigned ones.
   ```bash
   GIT_USER=$(gh api user --jq .login)
   gh issue list --state open --limit 50 --json number,title,author,createdAt,milestone \
     --jq "map(select(.author.login != \"$GIT_USER\")) | map(select(.milestone == null)) | .[] | \"#\(.number)|\(.author.login)|\(.title)|\(.createdAt[:10])\""
   ```
   Behavioral rules (mirror the contributor-PR step):
   - Render as a table with columns: issue #, author, title, opened-on date.
   - If any rows appear, **call them out at the top of the final response** alongside (or below) the contributor-PR call-out — e.g., "⚠️ N untriaged contributor issue(s) need attention."
   - If the same author appears multiple times, group them together in the table.
   - If zero rows, say "No untriaged contributor issues" for positive confirmation.

7. **Determine the current milestone.** Look at the most recent closed PR's milestone, or find the earliest open milestone:
   ```bash
   gh api repos/:owner/:repo/milestones --jq 'sort_by(.due_on // .title) | map(select(.state == "open")) | .[0].title'
   ```

8. **List open issues** on that milestone:
   ```bash
   gh issue list --milestone "<milestone>" --state open
   ```

9. **Display results** in this order:
   - Merge confirmation (which PR landed, what was authored by whom)
   - Contributor-PR section from step 5 — call-out if any exist, or "No open contributor PRs"
   - Untriaged contributor issues section from step 6 — call-out if any exist, or "No untriaged contributor issues"
   - Current milestone + its open issues as a formatted table (issue #, title, labels)
