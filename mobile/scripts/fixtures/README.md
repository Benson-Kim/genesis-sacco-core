# Guard-sweep falsifiability fixtures

Each `.dart.fixture` file is a DELIBERATE violation of one sweep in
`../guard_sweeps.sh`. The sweep runs against its own fixture and must reject
it; if it does not, the sweep has been broken (a typo in a pattern, a scope
narrowed too far) and the pipeline fails with that as the reason.

The extension is `.dart.fixture`, not `.dart`, so the analyzer never compiles
them and the sweeps' own source scan skips the directory.
