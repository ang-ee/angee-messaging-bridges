These responses are invented YouTube Data API v3 contract examples. Channel,
video, comment, author, and paging identities are synthetic; there are no OAuth
credentials or personal data.

`contract.json` separates upload, top-level comment, and reply pages. Embedded
replies deliberately contain only a subset, and thread resource ids differ from
their top-level comment ids. The examples cover private uploads, an old video,
missing author identity, held comments, and a reply authored by the connected
channel. Tests stub the shared integrate HTTP boundary and land parsed posts
through real integrate streams and the framework landing owners. Refusal examples
contain only machine-readable codes for expired page cursors, revoked grants,
throttling and daily quota.
Activity and history retain independent stream cursors; embedded replies are
never treated as a complete reply page.

The old activity page has another provider page behind it, so tests prove the
first pass stops at its live/age bound. The archive video has zero comments, so
history does not spend a thread call on it. Owner moderation reads, scoped token
resets, absent resources, reserved reply units, fixed request timeouts, and
partial pages at the engine deadline are exercised at the shared HTTP boundary.
