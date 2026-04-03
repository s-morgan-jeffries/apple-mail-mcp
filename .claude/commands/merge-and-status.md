Merge the current PR, pull main, and show open milestone issues.

## Steps

1. Find the open PR for the current branch
2. Wait for CI checks to pass (`gh pr checks <number> --watch`)
3. Squash merge the PR and delete the branch
4. Switch to main and pull
5. Determine the current milestone
6. List open issues on that milestone
7. Display results as a formatted table
