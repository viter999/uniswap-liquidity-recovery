# FREEZE MANIFEST — preregistration freeze of v0.3 (17 October 2026)

This is the list of files hashed at the preregistration freeze. Two hashes are given per file: sha256 of the bytes as stored,
and sha256 of the content with CRLF normalised to LF. Where the two are equal the file was stored with LF line endings.

The preregistration is published in this repository as `PREREG_uniswap_recovery_v0.3-public.md`, a copy of the frozen file
with four redacted lines (see `REDACTIONS.md` for the line numbers, both hashes and the statement that only those lines differ).
The outage list is published unchanged. The probe, proxy and query files are held privately and will be published with the
code at the code freeze; their hashes are listed so that the later publication can be checked against the freeze.

| file | bytes | sha256 (as stored) | sha256 (LF-normalised) |
|---|---|---|---|
| `PREREG_uniswap_recovery_v0.3.md` (frozen; private copy) | 36261 | `8d4b9bfc502c0baccb99fafa4d070d7446bd944e7c975ade5a596f0ff19a9a16` | `8d4b9bfc502c0baccb99fafa4d070d7446bd944e7c975ade5a596f0ff19a9a16` |
| `outages/outage_list_20261017.md` (published) | 4709 | `2f6115aa761b4643118442d019db1c922c2541e1d8d10c5af325501366c9d5e4` | `2f6115aa761b4643118442d019db1c922c2541e1d8d10c5af325501366c9d5e4` |
| `probes/probe_rpc.py` | 6958 | `e0029d33abb5aaf7d77f0118f0bc37e2cd631239755b7d8c8ba9d25ded4abbd2` | `e0029d33abb5aaf7d77f0118f0bc37e2cd631239755b7d8c8ba9d25ded4abbd2` |
| `probes/probe_rpc_alt.py` | 4680 | `67971bcb4bd65d863bdac172bfb5af21fe6006f7ea20a2b6381ce28ff3d73176` | `67971bcb4bd65d863bdac172bfb5af21fe6006f7ea20a2b6381ce28ff3d73176` |
| `probes/probe_20261017.json` | 6661 | `9c49f3f7cbec9da24a150ae6db3b673cc4fac5c7f51fa70218155026d4700fc4` | `c990f27f2bfd0656ba77b805da187f821a896b329ff7f14584d972e363bc721a` |
| `probes/probe_alt_20261017.json` | 4965 | `8eb8546be3bb6bfc762787c949f84ad626bfb5f32a892f3ba79d8bc2ebaef703` | `425327c82753afb7d95418d04d2ca739d2e032afb3ca4dbc34aa841cbdeb22b7` |
| `probes/probe_drpc_20261017.txt` | 690 | `d5e1226391d78c67a72bb7a9e64ada3ed4695a5bc2501a252a8d3c17dd0fd779` | `d5e1226391d78c67a72bb7a9e64ada3ed4695a5bc2501a252a8d3c17dd0fd779` |
| `probes/probe_genesis_20261017.txt` | 478 | `7cb14d19bf37f12834ce8948c20f71d9b0056382cb42bda1fd1eba62693c17a7` | `7cb14d19bf37f12834ce8948c20f71d9b0056382cb42bda1fd1eba62693c17a7` |
| `proxy/proxy_episode_count.py` | 7850 | `eea2a3b346f37e3463edd2848cc426010e33f281c129cc3970fea74b18060fea` | `eea2a3b346f37e3463edd2848cc426010e33f281c129cc3970fea74b18060fea` |
| `proxy/proxy_summary.json` | 7401 | `157e8769acb8fd9ed55f40a11ecce5628e6ccd1e315524a2beb36d9e4f8e9f79` | `60def76605334fb1fa807c8302b153705f933f3b3dc950553b0515f1b316bb55` |
| `bigquery/feasibility_episode_count.sql` | 6376 | `c33397de6181898bed341fe6f8381dc95d657944133e38b34346038590bb2d26` | `c33397de6181898bed341fe6f8381dc95d657944133e38b34346038590bb2d26` |

The private run log was hashed at the entry following the freeze entry (sha256 of its bytes as stored at that moment:
`b7eaea8db2ff7aba6bedd383c4bdbbebb9dd863aabc55beadc431bc948ed321b`), so the freeze entry is never self-referential. No git commit
hash exists for the freeze; the sha256 list is the anchor. Any later change to the preregistration is a new version number,
never an edit of v0.3.
