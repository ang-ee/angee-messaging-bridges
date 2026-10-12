# Facebook Page feeds

Connect one managed Page to ingest its public posts, comments, and replies, and
publish Page replies through Angee's shared delivery and moderation owners.
The deployment configures its Meta application keys on the Facebook OAuth client.

Connection discovery currently has no Page-choice contract. Grant the app access
to exactly one managed Page, including when the account manages several Pages;
discovery refuses grants exposing multiple Pages before applying any identity.
The original discovery credential retains the application identity after the Page
token is attached. Every request recomputes `appsecret_proof` using the current
confidential client secret. FK descriptors cache the registration within one
sync cycle; a fresh cycle observes secret rotation without reconnecting.

Activity polls comment counts within its live/active window. A separate
`reobserve` stream re-reads the active window (30 days by default), observing
edits, hides/unhides and deletion-plus-insertion changes with unchanged counts.
It spends at most `reobserve_units_per_cycle` API units per cycle (100 by default;
zero disables it), sharing the daily polling quota and protecting the reply
reserve. Budget exhaustion retains its cursor for the next cycle while live
ingestion continues. Completed walks wait one hour before starting again.
Each page and pending queue is bounded to 100 records. History walks all
available Page posts independently.
Missing comments do not prove deletion and are not automatically trashed.

The shared `FeedBackend` owns configuration, reserve validation, daily quota and
delivery settlement. Reads cost one unit in the integration-scoped UTC ledger;
publication costs one. Reconciliation prepays ten units for at most ten pages of
100 replies, newest first, and matches Page author, top-level comment parent,
exact body and the framework-supplied inclusive claim-time bound. Fresh replies
never search before posting. The first reconciliation waits for the framework's
three-minute visibility margin. Incomplete lookups raise. A recent Page-authored
reply on the same anchor with a differing body also remains uncertain, because
Graph may normalize it; a completed walk or time cutoff establishes absence
only when no such candidate exists.

Graph 5xx, codes 1/2 and `is_transient=true` retain comment work for retry; reply
POSTs with those ambiguous outcomes become uncertain. Connect/DNS/TLS failures
use integrate's typed not-sent error. Retry horizons combine the shared delta/date
parser, business recovery minutes, and a conservative one-hour cooldown when
Page/app utilization reaches 100%; that cooldown is local policy, not a Meta ETA.
Rate codes 4/17/32 without response hints retain a 15-minute provider default.
HTTP classification uses the framework's `request_json` helper; a non-JSON 4xx
is a definite refusal. Quarantine uses framework `RecordRefusal` records keyed
by `external_key` within the page transaction.
