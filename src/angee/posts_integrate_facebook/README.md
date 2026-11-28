# Facebook Page feeds

Connect one managed Page to ingest its public posts, comments, and replies, and
publish Page replies through Angee's shared delivery and moderation owners.
The deployment configures its Meta application keys on the Facebook OAuth client.

Connection discovery currently has no Page-choice contract. Grant the app access
to exactly one managed Page, including when the account manages several Pages;
discovery refuses grants exposing multiple Pages before applying any identity.
A Page access token can be entered instead, as a static token stored for the
Facebook OAuth client: discovery asks Graph's `me`, which a Page token resolves to
its Page, and records that Page as OAuth discovery does. The token must be the
Page's own: the same read asks for `category`, a field only Pages carry. A
person's token (whose `me` is the person) is refused before anything is recorded,
with "This token belongs to a person, not a Page: enter the Page's access token.",
only on Graph's own evidence: an answer without `category`, or Graph's refusal of
it on a User node. Any other refusal of `category` asks `me` again without it and
connects as before the check, Graph's refusal of a person's streams remaining the
backstop. The Page-only refusal relies on a Page's own token reading its
`category` at `me?fields=id,name,category`, which has not yet been confirmed on
a live Page connection. An answer that names no Page is refused, never taken for
the feed's Page. A feed stays on the Page its
first discovery recorded: a re-entered or newly attached token, or a grant, for
another Page fails discovery with "This feed is connected to another Facebook
Page; create a separate feed.", and the feed's streams and posts stay as they were.
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
HTTP classification uses the framework's `request_json` helper and integrate's
refusal: the adapter names only Graph's own cases (a rejected token, its rate
codes on any status, a refused cursor, a Graph error, one record's refused
read, a token's refused `category` read); any other status is integrate's
rule, so a 429 without a Graph rate code
is transient and waits this adapter's horizon, and a 4xx without a Graph error
is a definite refusal in integrate's words. Quarantine uses framework `RecordRefusal` records keyed
by `external_key` within the page transaction.
