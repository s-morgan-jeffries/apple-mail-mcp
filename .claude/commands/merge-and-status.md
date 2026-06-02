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

5. **Surface open contributor PRs** (any PR the current GitHub user did not author). This is the catch-net for external contributions that might otherwise sit unreviewed for weeks. The block below is hardened against a transient empty response that once hid PR #246 across four runs (#253): it echoes the filter identity, retries once on an empty fetch, reports total-vs-filtered counts, and filters via `jq --arg` (so an empty identity over-surfaces rather than silently dropping everything):
   ```bash
   GIT_USER=$(gh api user --jq .login)
   [ -z "$GIT_USER" ] && echo "WARNING: gh api user returned empty — contributor filter unreliable."
   echo "Identity for filter: ${GIT_USER:-<empty>}"
   prs=$(gh pr list --state open --json number,title,author,createdAt)
   if [ "$(printf '%s' "$prs" | jq 'length')" -eq 0 ]; then   # retry a transient empty (#253)
     sleep 1; prs=$(gh pr list --state open --json number,title,author,createdAt)
   fi
   total=$(printf '%s' "$prs" | jq 'length')
   contrib=$(printf '%s' "$prs" | jq --arg me "$GIT_USER" 'map(select(.author.login != $me))')
   echo "Open PRs: $total total, $(printf '%s' "$contrib" | jq 'length') by contributors"
   printf '%s' "$contrib" | jq -r '.[] | "#\(.number)|\(.author.login)|\(.title)|\(.createdAt[:10])"'
   ```
   Behavioral rules for displaying the result:
   - Render as a table with columns: PR #, author, title, opened-on date.
   - If any rows appear, **call them out at the top of the final response** — e.g., "⚠️ N contributor PR(s) need attention." Don't bury this below the milestone-issues list.
   - If the same author appears multiple times, group them together in the table.
   - Dependabot PRs count as contributor PRs for visibility purposes (they need rebase nudges or merges too).
   - If zero rows, say "No open contributor PRs" so the user gets positive confirmation rather than ambiguity.
   - **Report the count line.** A `total` > 0 with `0` contributor PRs is only expected when every open PR is the maintainer's; otherwise treat it as a possible dropped-filter miss and re-run before reporting "none." (#253)

6. **Surface untriaged contributor issues** (open issues filed by anyone other than the current GitHub user that are NOT assigned to a milestone). The reasoning for the no-milestone filter: assigned issues have already been triaged and are visible elsewhere — the truly invisible ones are the unassigned ones.
   ```bash
   GIT_USER=$(gh api user --jq .login)
   [ -z "$GIT_USER" ] && echo "WARNING: gh api user returned empty — contributor filter unreliable."
   echo "Identity for filter: ${GIT_USER:-<empty>}"
   issues=$(gh issue list --state open --limit 50 --json number,title,author,createdAt,milestone)
   if [ "$(printf '%s' "$issues" | jq 'length')" -eq 0 ]; then   # retry a transient empty (#253)
     sleep 1; issues=$(gh issue list --state open --limit 50 --json number,title,author,createdAt,milestone)
   fi
   total=$(printf '%s' "$issues" | jq 'length')
   untriaged=$(printf '%s' "$issues" | jq --arg me "$GIT_USER" 'map(select(.author.login != $me)) | map(select(.milestone == null))')
   echo "Open issues: $total total, $(printf '%s' "$untriaged" | jq 'length') untriaged contributor"
   printf '%s' "$untriaged" | jq -r '.[] | "#\(.number)|\(.author.login)|\(.title)|\(.createdAt[:10])"'
   ```
   Behavioral rules (mirror the contributor-PR step):
   - Render as a table with columns: issue #, author, title, opened-on date.
   - If any rows appear, **call them out at the top of the final response** alongside (or below) the contributor-PR call-out — e.g., "⚠️ N untriaged contributor issue(s) need attention."
   - If the same author appears multiple times, group them together in the table.
   - If zero rows, say "No untriaged contributor issues" for positive confirmation.
   - **Report the count line.** A `total` > 0 with `0` untriaged contributor issues is only expected when every open issue is the maintainer's or already milestoned; otherwise treat it as a possible dropped-filter miss and re-run. (#253)

7. **Determine the current milestone.** Look at the most recent closed PR's milestone, or find the earliest open milestone **by semantic version**. Sort on the numeric version tuple, not the title string — a lexical sort puts `v0.10.0` before `v0.9.0` (`'1' < '9'` at the third character). The `test(...)` guard sorts any non-`vX.Y.Z` title last so a future named milestone doesn't crash `tonumber`:
   ```bash
   gh api repos/:owner/:repo/milestones \
     --jq 'map(select(.state == "open"))
           | sort_by(.title
                     | if test("^v?[0-9]+(\\.[0-9]+)*$")
                       then (ltrimstr("v") | split(".") | map(tonumber))
                       else [999999] end)
           | .[0].title'
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
