# What is stored, who can read it, how to delete it (plan §8 item 6)

*Template for the tester-facing statement. Every sentence below is made true by the backend; edit
the bracketed parts to match your deployment before the first external tester.*

**What is stored.** Every entry you write, exactly as you wrote it, with the local date and time you
wrote it. The journal's replies to you, stored separately and never merged into your entries. When you
ask the journal to look back, the observations it showed you and a verification record of how each one
was checked against your own words.

**Where.** In a [Postgres] database hosted at [provider/region]. Entry text and the journal's replies
are encrypted at rest with a key held by [who]. `/health` on the API reports `encryption_at_rest: true`
when this is on.

**Who can read it.** You, through the app and through export. [Names/roles] for the purpose of running
this test, and only in aggregate or when you report a problem. No third party. Your entries are sent to
[model provider] to generate replies and are subject to their [zero-retention / retention policy].

**What is never done.** Your entries are never edited or summarized in place by the system. If you edit
an entry, the original is kept alongside the new version. The journal never claims something about your
writing that it cannot point to in your own words.

**Export.** At any time, `GET /export?format=md` gives you the whole journal as dated Markdown, and
`?format=json` gives the machine-readable form. Replies from the journal are included only if you ask
(`include_ai=true`), and then as a separate section.

**Delete.** `DELETE /me` exports everything and then removes every row associated with you — entries,
replies, sessions, look-back reports and receipts, and event logs. Nothing is kept.

**What is measured.** Whether you write a second entry, whether you ask the journal to engage, whether
you come back. These are derived from timestamps in your own data; there is no analytics tooling and
no tracking beyond the API calls the app makes.
