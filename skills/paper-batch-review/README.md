# Paper Batch Review Skill Mirror

This directory is the version-controlled source for the deployed batch-review
skill. Deploy it to the runtime skill directory used by each server:

- local review host: `/home/library/.claude/skills/paper-batch-review`
- production host: `/opt/.claude/skills/paper-batch-review`

Runtime-specific absolute paths inside prompt templates must use the target
server's project and skill roots. The Python report generator itself resolves
the project root from `PAPER_INTEGRITY_ROOT`.

The public PDF renderer uses compact reports by default. Set
`REVIEW_REPORT_VERBOSE=1` only for debugging historical output.
