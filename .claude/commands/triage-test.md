Run a one-off triage against a real package to test the Scout locally.

Parse $ARGUMENTS for ecosystem, package, old version, and new version. If any are missing, ask the user. Repo and PR number can be anything (they only affect whether real PR actions fire, which is off by default).

Then run:

```bash
uv run python -m start_workflow \
  --repo owner/test-repo \
  --package {package} \
  --old-version {old} \
  --new-version {new} \
  --pr-number 1
```

If the user specified an ecosystem other than pip, add `--ecosystem {ecosystem}`.

Remind them to open http://localhost:8233 to watch the workflow run in the Temporal UI, and that Temporal must be running (`temporal server start-dev` in a separate terminal) for this to work.

After the command returns, show the verdict that was printed to stdout.
