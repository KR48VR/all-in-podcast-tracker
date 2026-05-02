# Known gaps

A small number of episodes in the archive don't have AI-generated summaries
because their transcripts weren't available when they were processed. The
fetcher couldn't pull a posted transcript from allin.com, and Whisper either
wasn't run on the audio or didn't succeed at the time.

These episodes are still listed in the archive (with title, date, and links),
but their cards show no summary text. Most are several years old and weren't
worth chasing transcripts for.

## Episodes without summaries

| Date | Title |
|---|---|
| 2025-05-09 | Fed Hesitates on Tariffs, The New Mag 7, Death of VC, Google's Value in a Post-Search World |
| 2023-03-11 | Silicon Valley Bank implodes: startup extinction event, contagion risk, culpability |
| 2023-03-03 | AI FOMO frenzy, macro update, Fox vs Dominion, US vs China & more with Brad Gerstner |
| 2023-02-24 | Did Stripe miss its window? Plus: VC market update, AI comes for SaaS, Trump's savvy |
| 2023-02-17 | Toxic out-of-control trains, regulators, and AI |
| 2023-02-11 | The AI Search Wars: Google vs. Microsoft, Nordstream report, State of the Union |
| 2022-01-15 | Insurrection indictments, human rights in the US and abroad, groundbreaking AI study |

To re-process any of these, fetch a transcript manually (from YouTube captions,
audio re-transcription, or a posted transcript) and re-run
`python src/analyze_episode.py`.
