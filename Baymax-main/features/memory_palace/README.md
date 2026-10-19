# Memory palace

This core feature will maintain a connected understanding of each person: people,
relationships, interests, experiences, goals, plans and the evidence connecting
them. It will supply relevant context to conversations, follow-ups and toolkits.
It is a sibling of person recognition and toolkits, not a news-feed component.

**Status:** architecture namespace only. No graph store, automatic learning,
inference, retention scoring or graph UI is implemented in this folder yet.

See the [memory-palace design](DESIGN.md) for identity ownership, evidence/graph
records, learning/correction, scoped recall, feed integration, inspectability,
forgetting, implementation order and acceptance scenarios. It includes the source
investigation of the reported James identity mix-up without claiming a confirmed cause.

The integration starting point is open PR #8's person-linked memories in
`person_memory.py`. Reuse its existing identity associations and forgetting flow;
extend them with explicit relationship/evidence semantics instead of creating
another independent memory database.

Planned inputs are attributed conversation evidence, confirmed profile facts and
explicit corrections/feedback. Outputs are relevant memory context and explainable
interest/context signals. Each connection needs a source, time and confidence;
tentative inferences must remain distinguishable from confirmed facts. The user
should be able to inspect, correct and forget what is retained.

Dependencies, persistence, provider data flows, enablement and end-to-end learning
tests will be specified with the implementation. This namespace has no runtime
workers, external requests, data files or enablement switch.
