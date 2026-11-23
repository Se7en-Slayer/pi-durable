# Herdr learning handoff

This role-neutral candidate retains what worked, what went wrong, and what to
carry forward for a specific assignment and its evidence. The stand-down guard
checks a trusted observation and independently accepted learning before it can
settle a worker tab. Legacy receipts require a separately attributed supplement.

Run on Node 22:

    node --test tools/herdr-learning/stand-down.test.mjs

The tests use isolated fixtures and fake Herdr commands. This draft does not
install the runtime or supply a production observer.

The independently reviewed ten-minute idle reflection candidate is under
`reflection/`, with the shared runtime files needed to run its conformance tests.
It resolves agents and subagents from registered identity and assignment data.
Guarded delivery supports Idle sessions. Done sessions remain queued for CoS
follow-up because the installed transport has no guarded Done send.

    cd tools/herdr-learning/reflection
    UO_SOURCE="$PWD" PYTHONPATH="$PWD" python3 -B -m unittest discover -s tests -v

See `reflection/REFLECTION.md` for the contract and remaining limits.
