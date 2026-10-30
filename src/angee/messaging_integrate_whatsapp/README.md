# WhatsApp


## Status updates and retained databases

WhatsApp's `status@broadcast` container carries independent updates by their
actual authors. It is classified as Messaging's `status` modality, rather than a
group conversation; ordinary inbox coverage excludes it unless updates are
selected. Message IDs, authors, source accounts, content and attachments retain
their original identity.

For a retained database with status containers classified as groups, author a
normal project migration after the new modality's native schema migration. The
historical upgrade asset at
`compose_migrations/messaging/classify_status_feeds.py` supplies the narrowly
scoped, idempotent `forwards` operation. Add it through native Django `RunPython`
in the project's reviewed history; compose does not discover or schedule this
asset. The operation matches only complete WhatsApp status source keys, including
older numeric channel prefixes, and leaves other broadcasts unchanged. Preserve
any helper already imported by retained project migrations.
