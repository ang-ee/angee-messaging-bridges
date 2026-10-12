# YouTube reference-derived fixtures

Every fixture in `contract.json` is derived from the documented API reference
(https://developers.google.com/youtube/v3/docs/), not recorded. Identities, text,
ETags and paging tokens are synthetic. No configured OAuth client was used.

Follow-up: record sanitized payloads once an operator connects.

The envelopes and requested resource parts follow the references for
[channels.list](https://developers.google.com/youtube/v3/docs/channels/list),
[playlistItems.list](https://developers.google.com/youtube/v3/docs/playlistItems/list),
[videos.list](https://developers.google.com/youtube/v3/docs/videos/list),
[commentThreads.list](https://developers.google.com/youtube/v3/docs/commentThreads/list),
[comments.list](https://developers.google.com/youtube/v3/docs/comments/list),
[comments.insert](https://developers.google.com/youtube/v3/docs/comments/insert), and
[API errors](https://developers.google.com/youtube/v3/docs/errors).

The [thread resource](https://developers.google.com/youtube/v3/docs/commentThreads)
exposes a thread `id` and `snippet.topLevelComment.id`; real responses use the
same value for these identities. Reply publication and
`comments.list(parentId=...)` use the top-level comment id. Embedded replies
are a subset. Tests may deliberately vary the ids to pin field selection;
that is a fault injection, not a claim that real ids differ.
The [comment resource](https://developers.google.com/youtube/v3/docs/comments)
supplies `textOriginal` only to its author and omits `moderationStatus` on ID
reads. Visitor examples use `textDisplay`; owner replies include original text.

Tests deliberately construct invalid records and cursor loops from these
envelopes. Those fault injections are not claims about successful API responses.
