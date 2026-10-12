# Facebook reference-derived fixtures

Every fixture in `graph.json` is derived from the documented API reference
(https://developers.facebook.com/docs/graph-api/), not recorded. Identities, text,
trace ids and credential placeholders are synthetic; no OAuth client was used.

Follow-up: record sanitized payloads once an operator connects.

Resource and pagination shapes follow the references for
[Page posts](https://developers.facebook.com/docs/graph-api/reference/page/posts/),
[object comments](https://developers.facebook.com/docs/graph-api/reference/object/comments/),
[managed Pages](https://developers.facebook.com/docs/graph-api/reference/user/accounts/),
[long-lived grants](https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived/),
[error envelopes](https://developers.facebook.com/docs/graph-api/guides/error-handling/), and
[rate limits](https://developers.facebook.com/docs/graph-api/overview/rate-limiting/).

Top-level comments omit `parent`; replies include their parent comment id.
Comment ids have the `{post}_{comment}` form. Post comment summaries retain
an empty `data` array alongside `summary` for the zero-limit expansion.
Comment records include `created_time`, Page/visitor author identity,
and `is_hidden`. `server_transient` models a Graph 5xx with code 2 and
`is_transient=true`; throttling examples include business recovery minutes and
Page/app utilization headers. Tests also inject code 1 and transient flags on
other codes. Utilization percentages are not recovery-time estimates.

Post and comment walks use independent cursors. Untrusted `paging.next` links
are deliberate security fault injections; requests retain only `cursors.after`
and use the preset's fixed Graph origin. Malformed record fixtures are deliberate
fault injections, not documented successful responses.
