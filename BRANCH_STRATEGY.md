# Branch Strategy

`main` preserves the shared, validated history through Phase 3A and serves as the
integration baseline. It is not the normal branch for personal or product development.

`internal-tool` is the default branch and the primary home for the personal Internship
Signal tool: watcher collection, personal scoring and ranking, alumni-matching code,
evaluation tooling, and the existing scheduled workflow.

`product-mvp` is the hosted multi-user product branch for accounts, PostgreSQL job
storage, per-user matching, the dashboard, and hosted notifications. Phase 3B remains
paused until separately authorized.

Both branches initially retain the same application code. This structure establishes
ownership and future development boundaries; it does not make `internal-tool` private.
Private data, profiles, evaluation results, generated private artifacts, state files,
recipient addresses, and credentials must never be committed to either branch.

## Shared-fix transfer policy

1. Never merge `internal-tool` wholesale into `product-mvp`.
2. Never merge `product-mvp` wholesale into `internal-tool`.
3. Put reusable watcher or core fixes in commits that contain no personal data,
   product-only changes, or unrelated edits.
4. Review the complete commit diff before transfer.
5. Transfer an approved shared commit with `git cherry-pick <sha>`.
6. Run the relevant tests on the destination branch after transfer.
7. Do not cherry-pick commits touching private or generated files into `product-mvp`.
