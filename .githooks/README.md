# Versioned git hooks

`pre-commit` runs a gitleaks staged-secret scan and blocks a commit whose staged
changes contain a detectable secret (round-6 G6: free push-protection replacement
for private repos).

Activate after cloning:

    git config core.hooksPath .githooks

Requires gitleaks (`brew install gitleaks`). If gitleaks is absent the hook fails
open (skips the scan) so it can never wedge commits on a bare machine.
