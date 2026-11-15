# Official contracts consulted

Checked on 12 November 2026. These references informed implementation; live accounts were not supplied at build time.

- [HackerOne Hacker API resources](https://api.hackerone.com/hacker-resources/): `/v1/hackers/hacktivity` and `/v1/hackers/me/reports`, Basic authentication, page-number pagination.
- [HackerOne object reference](https://api.hackerone.com/hacker-reference/): report attributes, program/severity relationships, and pagination links.
- [Bugcrowd Hacker API authentication](https://docs.bugcrowd.com/hacker-api/authentication/): `Authorization: Token ID:secret` and read scopes.
- [Bugcrowd Hacker API usage](https://docs.bugcrowd.com/hacker-api/usage/): offset/limit pagination, flat submission summaries, and rate limiting.
- [Bugcrowd v1.0.0 OpenAPI](https://docs.bugcrowd.com/hacker-api-specs/hacker-api-1.0.0.yml): engagement and submission schemas, production host.
- [Intigriti Researcher API guide](https://kb.intigriti.com/en/articles/8529303-intigriti-researcher-api) and [official v1.0 OpenAPI](https://api.intigriti.com/external/researcher/swagger/v1.0/swagger.json): Bearer authentication, `/v1/programs` and `/v1/programs/activities` under `https://api.intigriti.com/external/researcher`, `records`/`maxCount` envelopes, and offset/limit pagination. Checked against authorized live responses on 13 November 2026. Live activity `createdAt` values are Unix seconds; millisecond-scale values are rejected. The schema has no activity ID and leaves activity details abstract, so content-derived identities and a documented recent window are used. No payout API is called.
- [Discord webhook resource](https://docs.discord.com/developers/resources/webhook): execution payload and `wait=true`.
- [Discord rate limits](https://docs.discord.com/developers/topics/rate-limits): cooldown headers and `retry_after` seconds.
- [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site): static hosting, without server-side Python.
- [Render persistent disks](https://render.com/docs/disks): persistence requirements and single-instance disk constraints.

Provider content is treated as untrusted data. Requests use fixed official hosts and documented paths, TLS verification, explicit timeouts, and no redirects. Pagination URLs are validated and only their numeric page is accepted. No credential is forwarded to a URL supplied by an API response.

On 14 November 2026, authorized live domain-scope activities confirmed type ID 1 (`New domains version added`) with `fromVersionId` and `toVersionId`. The documented `/v1/programs/{programId}/domains/{versionId}` returned both historical versions successfully. Program details provide the current domain version. Live URL/Wildcard type IDs were 1/7; Out Of Scope tier was 5; an open program used status ID 3. The parser checks both IDs and labels and excludes unknown asset kinds/tiers. [Bounty tiers](https://kb.intigriti.com/en/articles/5991807-bounty-table-tiers) confirms that No Bounty assets are not automatically out of scope. Exact endpoint strings are retained; listed targets are never requested by the monitor.
