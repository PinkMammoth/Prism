# Implementation and evidence commit provenance

The implementation is now integrated into the existing `main` history in two focused commits:

| Scope | Commit on main | Original isolated release |
|---|---|---|
| Lab backup registration, fingerprint/frontier manifests and standard off-host lifecycle | `56cafcf` | `9acb42a94d8888a2e712490b421912739a02bff6` |
| Rehearsal-only Lab v2 profile, execution fences and guarded preparation | `09a0a21` | `eee205f24d5d9ef3cd71c32499f13ec2e2fbb8fd` |

The two original release identities remain accurate historical deployment/preparation evidence.
The backup hotfix was deployed under `9acb42a`; the isolated prepared v2 release was not deployed.
Porting the implementation onto current main preserves main's existing Research profile and
history. Do not treat a main commit as the identical whole release image to its older isolated
counterpart. The original exact prepared release is recoverable from `lab-releases.bundle`.

A third commit records the completed reports, reviewed readiness/resource/rollback preparation,
base and targeted-window evidence, audit scripts and validation results. The resource proposal
remains explicitly unvalidated, the Research backup alert remains recorded, and no activation or
authority change is implied by committing these files.

The secret scan covered all 113 report/evidence files (including the decompressed journal),
found no known local secret value or private-key marker, and excluded Python bytecode caches.
No `.env`, backup database, local archive or `.worktrees/` checkout belongs to these commits.
Unrelated working-tree changes remain outside all three commits. No VPS operation or push is
part of this commit-only task.

Current-main integration verification passed **165 tests** in326.94seconds, plus Ruff and shell
syntax checks. See `main-integration-validation.json` and `main-integration-regressions.log`.
Raw evidence whitespace is intentionally preserved; code/report Markdown passes the diff check.
