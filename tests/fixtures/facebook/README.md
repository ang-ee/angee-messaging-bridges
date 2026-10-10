# Synthetic Facebook Page contracts

`graph.json` contains invented Graph responses for one Page. All names,
identities, and credential placeholders are synthetic. These are protocol-shape
fixtures, not sandbox captures.

Post and comment pagination use independent cursors. Next links deliberately
contain an untrusted origin and a synthetic credential placeholder; callers must
construct requests from the fixed Graph origin and retain only `cursors.after`.
Comments include a reply before its parent, an author omitted by the provider,
a Page-authored reply, and a hidden comment. Stream-count summaries permit activity
tests on old posts. The throttle fixture includes code 80001 and Meta's business
usage recovery estimate in minutes. The existing reply models a lost publish acknowledgement.

The contract should be verified against Meta's Page posts, object comments,
long-lived access-token, and rate-limiting API documentation with an approved
sandbox app at integration time. No live credentials or personal data belong here.
