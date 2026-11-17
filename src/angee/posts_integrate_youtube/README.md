# YouTube channel feeds

A channel connects through the deployment's one `google` OAuth client
(`angee.integrate_google`), asking for `youtube.force-ssl` on top of the client's
sign-in scopes; enable the YouTube Data API v3 in that client's Cloud project.

Upgrading from the addon's own `youtube` client: author a native data migration
that runs `move_youtube_client` from
`compose_migrations/integrate/move_youtube_client_to_google.py`. It renames the
`youtube` row to `google` with `provider_type = google` and the preset's
catalogue, authorize parameters and revoke endpoint, keeping its id, so channels
keep their credentials and need no reconnect. It refuses when a `google` client
already exists in the same environment. Until it runs, rows holding the retired
`google_youtube` key still load.

The channel feed uses the shared `FeedBackend` configuration, quota and delivery
owners. The provider config extends the base with a 1,000-unit reply reserve and
`recheck_interval` (at least one hour). Discovery stores the uploads playlist in
the read-only feed extension. History, live activity and re-observation each have
their own durable stream position.

Quota scope is the OAuth client row identity, shared across its channels, with
daily reset in `America/Los_Angeles`. Configure one client identity per Cloud
project budget and the same quota policy for its feeds. List operations cost one
unit; reply publication costs 50. Owner re-observation of the 30-day active window
uses the shared `reobserve_units_per_cycle` allowance (100 units by default;
zero disables it), including moderation, identity, root and reply reads. A walk
that exhausts either that allowance or the daily polling quota retains its cursor
and resumes next cycle. Live ingestion continues independently. Completed walks
wait for `recheck_interval` before starting again. The project ledger protects the
reply reserve from all polling reads.

Published scans include the watermark second; ingest is idempotent. Held/spam
owner scans supply moderation evidence because comment ID reads omit it. After
both scans complete, an available previously hidden top-level comment that
neither listed is restored. Replies retain their previous hold because those
scans only cover top-level comments. Incomplete scans (including a 5,000-id
evidence cap) preserve prior holds; missing resources never prove deletion.
Framework `RecordRefusal` evidence travels with its page for the engine to
record transactionally, and overflow retains only safe identity/code evidence.

Fresh delivery posts the exact body on `message.top_level_comment()` and returns
the provider comment id. Integrate's typed not-sent connection refusal can be
retried; missing acknowledgements and post-send transport/5xx failures become
uncertain. Reconciliation runs only after the framework's settlement gate and
three-minute visibility margin, and matches channel author, parent, exact
original body and its inclusive time bound.
It prepays ten API units for a maximum ten pages of 100 replies. Because
[comments.list](https://developers.google.com/youtube/v3/docs/comments/list)
offers no ordering parameter, the bounded complete result is sorted newest first
before matching. An incomplete walk raises. The API documents no indexing
horizon proving a reply absent after an ambiguous send; unmatched lookups remain
uncertain for another observation or operator acknowledgement. An operator can
explicitly decide to retry through messaging.
