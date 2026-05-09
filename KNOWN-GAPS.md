# Known gaps

A small number of episodes in the archive don't have AI-generated summaries
because their transcripts weren't available when they were processed. The
fetcher couldn't pull a posted transcript from allin.com, and Whisper either
wasn't run on the audio or didn't succeed at the time.

These episodes are still listed in the archive (with title, date, and links),
but their cards show no summary text.

## Episodes without summaries

| Date | Title | Status |
|---|---|---|
| 2026-05-08 | Elon's Anthropic Deal, The Next AI Monopoly?, "FDA for AI" Panic | New this week — transcript may appear in a future run |
| 2025-05-09 | Fed Hesitates on Tariffs, The New Mag 7, Death of VC | Persistent — no transcript ever fetched |
| 2022-01-15 | Insurrection indictments, human rights in the US and abroad | Persistent — no transcript ever fetched |

## Recently resolved

These episodes were originally listed here but got analyzed in the
2026-05-09 weekly auto-run (the 60K → 20K transcript shrink + skip-empty
patch let them through):

- 2023-03-11 — Silicon Valley Bank implodes
- 2023-03-03 — AI FOMO frenzy, macro update
- 2023-02-24 — Did Stripe miss its window?
- 2023-02-17 — Toxic out-of-control trains, regulators, and AI
- 2023-02-11 — The AI Search Wars: Google vs. Microsoft

## How to re-process

To re-process any of these, fetch a transcript manually (from YouTube captions,
audio re-transcription, or a posted transcript) and re-run
`python src/analyze_episode.py`.
