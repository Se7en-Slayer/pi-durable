# UO-04 conformance fixtures

Run from this conformance lane, pointing `UO_SOURCE` at the isolated combined candidate (never installed entrypoints). The lane alone contains only the legacy implementation until integration:

```sh
UO_SOURCE=/absolute/isolated/candidate /usr/bin/sandbox-exec -p '(version 1) (allow default) (deny network*)' python3 -B -m unittest discover -s tests -v
/usr/bin/sandbox-exec -p '(version 1) (allow default) (deny network*)' python3 -B -m unittest discover -s tests -p test_graph_conformance.py -k LegacyCompatibility -v
```

`fixtures/legacy-a2-independent.json` is a static sanitized A2 compatibility shape derived from the frozen legacy reader and UO-01's recorded dual `state`/`status` and independent-candidate contract. `fixtures/legacy-report-closeout.json` represents the older complete-report path. All identifiers, fake SHAs, descriptions, and paths are synthetic and the path placeholders resolve inside `TemporaryDirectory`. No current goal payload or raw receipt is copied. The marked baseline legacy test replaces Git with expected argv/results and uses temporary evidence files; a tampered log must still fail. These fixtures do not certify byte-for-byte equivalence of a historical raw goal.

The v2 graph cases bind the published `build_graph`, `load_graph`, `view`, and `transition` API, not guessed CLI output. The current candidate gate must keep unversioned/v1 behavior; legacy docs are not upconverted by the graph loader. The synthetic graph deliberately uses a repeated task ID in two projects, artifact-backed (non-Git) handoffs, and a coordinator→Frontend→QA→Security ancestry. It checks independent exact-source/work/manifest/attempt acceptance, post-acceptance byte tampering, same-terminal logical alias, nested and filtered views, cross-project dependencies, invalid owners, orphan references, cycles, identity generation change, holds, settled reassignment, and successful transition of independent tasks despite malformed data. No case writes to a real goal file or starts the installed runtime.

`WatchdogCorrections` pins three previously failing boundaries: explicit CLI `--receipt` with JSON `null` versus a matching review object, CLI peer prerequisite resolution from the selected goal's own directory (with a missing-peer negative control), and serialization of the same exact authorized scope across projects with different terminal handles. The scope case also dispatches unrelated work while the first attempt is active, then dispatches the same-scope peer only after settlement. Run just these three with `-p test_graph_conformance.py -k WatchdogCorrections`.

## Runtime cases (transport and clock are fakes)

Runtime assertions bind only agreed UO-03 APIs. No real `herdr-core`, native case, or recovery helper may run. Use a throwaway state directory, fake terminal inventory with immutable `(terminal_id, created_ms)`, and record exact calls and JSON snapshots before/after. Fixtures model, rather than reproduce, uncertain delivery:

| Case | Setup and injected event | Required observation |
|---|---|---|
| Late owner hold / pause | Work initially ready, hold arrives at the last pre-send or pre-recovery recheck | No transport call and no new/replayed attempt; leave held work alone. Independently ready branch remains selectable where pause isn't global. |
| Uncertainty before send returns | Dispatch intent/checkpoint persisted; fake send records transport invocation then raises ambiguous delivery | Reload leaves task uncertain or active, does not repeat send or reuse attempt number. Reconciliation requires authoritative evidence. |
| Uncertainty after send returns | Transport accepts once, interruption before confirmed receipt/ledger update | Next scan must not replay; receipt/checkpoint boundary remains inspectable and unrelated work remains usable. |
| Terminal not observable | Active old owner disappears from observed inventory; no independent settlement proof | No automatic settlement or reassign, no recovery to a substituted/reused generation. A supported explicit proof can settle safely and preserves attempt history. |
| Distinct stale identity | Same terminal label reappears with a different generation immediately before a side effect | Refuse that side effect, without calling send/helper for the stale owner. |
| Bounded proven stall | Registered writer still same generation, stable idle and latest bound checkpoint, no hold | One scoped observation for CoS reconciliation against the current attempt and checkpoint; no automatic send or settlement. |
| Legacy report closeout | Unversioned goal with nonempty report and complete state | No recovery initiated. Empty/missing report retains legacy outstanding/reconcile behavior. |

A Fake Herdr pass is conformance evidence for the registered path only, not proof of real Desktop launch/native-case or out-of-band command governance.
