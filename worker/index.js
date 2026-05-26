/**
 * Cloudflare Worker: "Ask across all episodes" chat backend.
 *
 * Receives { question, episodes[] } from the site, pulls the most relevant
 * episode summaries/takeaways, and calls Groq (open-source Llama) with them
 * as context. Returns { answer, citations }.
 *
 * Secrets (set via `wrangler secret put`):
 *   GROQ_API_KEY
 */

const GROQ_URL = "https://api.groq.com/openai/v1/chat/completions";
const MODEL = "llama-3.3-70b-versatile";
const TOP_K = 12; // how many episodes to include as context

// Level 2: pull transcript excerpts for the top N episodes at chat time. These
// excerpts give the LLM real quoted material, not just the analyzed summary -
// so it can answer questions about specifics the analysis layer missed.
//
// Each tracker sets RAW_EPISODE_BASE in its own wrangler.toml under [vars],
// pointing at that tracker's per-episode JSON folder. This keeps the Worker
// code reusable across different podcast/lecture trackers without code edits.
const SNIPPET_TOP_N = 3;
const SNIPPET_BEFORE_CHARS = 600;
const SNIPPET_AFTER_CHARS = 1400;
const SNIPPET_FALLBACK_CHARS = 2000; // when no query term matched in transcript

export default {
  async fetch(request, env) {
    // CORS
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }
    if (request.method !== "POST") {
      return json({ error: "POST only" }, 405);
    }

    let body;
    try { body = await request.json(); }
    catch { return json({ error: "invalid json" }, 400); }

    const question = (body.question || "").slice(0, 2000);
    const episodes = Array.isArray(body.episodes) ? body.episodes : [];
    // Validate conversation history: must be array of {role, content} pairs.
    const history = Array.isArray(body.history)
      ? body.history
          .filter((h) =>
            h && (h.role === "user" || h.role === "assistant") && typeof h.content === "string"
          )
          .slice(-6) // hard cap on worker side too
          .map((h) => ({ role: h.role, content: h.content.slice(0, 4000) }))
      : [];
    if (!question) return json({ error: "missing question" }, 400);
    if (!env.GROQ_API_KEY) return json({ error: "GROQ_API_KEY not set" }, 500);

    // Build retrieval query from recent user turns + the new question so
    // follow-ups like "elaborate on that" inherit the previous topic and
    // any "latest episode" recency cue.
    const recentUserText = history
      .filter((h) => h.role === "user")
      .slice(-2)
      .map((h) => h.content)
      .join(" ");
    const retrievalQuery = (recentUserText + " " + question).trim();

    const relevant = rankEpisodes(retrievalQuery, episodes).slice(0, TOP_K);

    // Level 2: enrich the top SNIPPET_TOP_N episodes with a transcript excerpt
    // so the LLM has real quoted material, not just the analyzed summary.
    // Skip this step gracefully if RAW_EPISODE_BASE isn't configured.
    const terms = parseTerms(retrievalQuery);
    if (env.RAW_EPISODE_BASE) {
      const topForSnippets = relevant.slice(0, SNIPPET_TOP_N);
      const snippets = await Promise.all(
        topForSnippets.map((ep) => fetchTranscriptSnippet(env.RAW_EPISODE_BASE, ep.id, terms))
      );
      topForSnippets.forEach((ep, i) => { ep._snippet = snippets[i]; });
    }

    const context = relevant.map(formatEpisode).join("\n\n---\n\n");
    const citations = relevant.map((e) => `${e.date} · ${e.title}`);

    const systemPrompt =
      "You are an expert analyst of the All-In Podcast. Answer the user's " +
      "question using the episode notes below. " +
      "TRANSCRIPT_EXCERPT fields contain verbatim text from the recording. " +
      "When the user asks what someone said or how someone framed something, " +
      "the TRANSCRIPT_EXCERPT is your PRIMARY source. ALWAYS quote relevant " +
      "lines directly from it in your answer, even when uncertain about the " +
      "speaker. Transcripts have no explicit speaker labels, so when " +
      "attributing to a specific host (Chamath, Sacks, Friedberg, Jason), " +
      "use phrasing like 'likely Chamath, based on phrasing' or 'one of the " +
      "hosts (probably Sacks)' - but ALWAYS provide the actual quoted text " +
      "from the transcript anyway. Do NOT respond with 'this is not a direct " +
      "quote from X' as a reason to withhold material - the transcript IS " +
      "the verbatim recording, your job is to surface the words and " +
      "acknowledge attribution uncertainty separately. " +
      "If the notes truly don't contain the answer, say so plainly. " +
      "Keep answers tight and cite episode titles inline like (E. Title, YYYY-MM-DD). " +
      "When a takeaway or quote has a timestamp and youtube URL, include a " +
      "Markdown deep-link in the form [▶ mm:ss](youtube_url?t=SECONDSs) right " +
      "after the relevant sentence so the reader can jump to that moment. " +
      "Use the prior conversation turns to resolve pronouns and follow-ups " +
      "(e.g. 'that', 'he', 'the latest episode') and stay on the same thread.";

    const payload = {
      model: MODEL,
      messages: [
        { role: "system", content: systemPrompt },
        ...history,
        { role: "user", content: `Episode notes:\n\n${context}\n\nQuestion: ${question}` },
      ],
      temperature: 0.3,
    };

    let answer = "";
    try {
      const r = await fetch(GROQ_URL, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GROQ_API_KEY}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      answer = data.choices?.[0]?.message?.content
        || data.error?.message
        || "No response.";
    } catch (e) {
      return json({ error: "groq call failed: " + e.message }, 502);
    }

    return json({ answer, citations });
  },
};

/* ------ Helpers ------ */

function parseTerms(question) {
  return question
    .toLowerCase()
    .replace(/[^\w\s]/g, " ")
    .split(/\s+/)
    .filter((t) => t.length > 2);
}

function rankEpisodes(question, eps) {
  // Simple keyword score - cheap, deterministic, works well enough as a
  // retrieval step when the corpus is a few hundred summaries. Upgrade to
  // embeddings later if you want smarter retrieval.
  const terms = parseTerms(question);

  // Detect questions asking for the latest/newest content. When found, we
  // sort purely by date so retrieval doesn't accidentally surface an old
  // episode that happens to share keywords with the question.
  // Match plain recency words OR phrases like "last 10 episodes", "past
  // few weeks", "recent shows" so questions that name a count still trigger
  // a date-sorted retrieval rather than keyword similarity.
  const wantsRecent =
    /\b(latest|newest|most\s+recent|recently)\b/i.test(question) ||
    /\b(?:last|past|recent)\s+(?:\d+\s+|few\s+)?(?:episode|week|show)s?\b/i.test(question);

  const ranked = eps.map((ep) => {
    // Takeaways may be strings (legacy) or {text, timestamp_seconds}.
    const takeawayText = (ep.takeaways || [])
      .map((t) => (typeof t === "string" ? t : t.text || ""))
      .join(" ");
    const hay = [
      ep.title, ep.summary,
      (ep.topics || []).join(" "),
      takeawayText,
      (ep.guests || []).join(" "),
      (ep.notable_mentions || []).join(" "),
    ].join(" ").toLowerCase();
    let score = 0;
    for (const t of terms) if (hay.includes(t)) score += 1;
    // Boost recent episodes slightly so "latest" queries feel right.
    if (ep.date) {
      const daysOld = (Date.now() - new Date(ep.date).getTime()) / 86_400_000;
      score += Math.max(0, 1 - daysOld / 365);
    }
    return { ...ep, _score: score };
  });

  if (wantsRecent) {
    return ranked.sort((a, b) => (b.date || "").localeCompare(a.date || ""));
  }
  return ranked.sort((a, b) => b._score - a._score);
}

function formatEpisode(ep) {
  const fmtTake = (t) => {
    if (typeof t === "string") return `- ${t}`;
    const ts = t.timestamp_seconds != null ? ` [t=${t.timestamp_seconds}s]` : "";
    return `- ${t.text || ""}${ts}`;
  };
  const fmtQuote = (q) => {
    if (!q) return "";
    const ts = q.timestamp_seconds != null ? ` [t=${q.timestamp_seconds}s]` : "";
    return `- "${q.text || ""}" - ${q.speaker || "Unknown"}${ts}`;
  };
  const lines = [
    `EPISODE: ${ep.title}`,
    `DATE: ${ep.date}`,
    ep.youtube_url ? `YOUTUBE_URL: ${ep.youtube_url}` : "",
    ep.guests?.length ? `GUESTS: ${ep.guests.join(", ")}` : "",
    ep.topics?.length ? `TOPICS: ${ep.topics.join(", ")}` : "",
    ep.notable_mentions?.length ? `NOTABLE_MENTIONS: ${ep.notable_mentions.join(", ")}` : "",
    "",
    `SUMMARY: ${ep.summary || ""}`,
    "",
    "TAKEAWAYS:",
    ...(ep.takeaways || []).map(fmtTake),
    (ep.quotes || []).length ? "\nQUOTES:" : "",
    ...(ep.quotes || []).map(fmtQuote),
    ep._snippet ? `\nTRANSCRIPT_EXCERPT (verbatim from the recording):\n${ep._snippet}` : "",
  ].filter(Boolean);
  return lines.join("\n");
}

async function fetchTranscriptSnippet(rawBase, epId, terms) {
  // Fetch the raw per-episode JSON from a configurable base URL, with edge
  // cache. We cache aggressively because episode JSONs only change on weekly
  // workflow runs - so a 1-hour cache is comfortably safe.
  if (!epId || !rawBase) return "";
  const url = `${rawBase}/${epId}.json`;
  try {
    const cache = caches.default;
    const cacheKey = new Request(url);
    let resp = await cache.match(cacheKey);
    if (!resp) {
      resp = await fetch(url, { cf: { cacheTtl: 3600, cacheEverything: true } });
      if (resp.ok) {
        const cloned = resp.clone();
        const cacheHeaders = new Headers(cloned.headers);
        cacheHeaders.set("Cache-Control", "public, max-age=3600");
        const cacheable = new Response(cloned.body, {
          status: cloned.status,
          headers: cacheHeaders,
        });
        // Fire-and-forget cache put; don't await so chat latency stays low.
        cache.put(cacheKey, cacheable).catch(() => {});
      }
    }
    if (!resp.ok) return "";
    const data = await resp.json();
    const transcript = data.transcript || "";
    return extractSnippet(transcript, terms);
  } catch (e) {
    // Never let snippet fetch failure break the chat - just return empty
    // and let the rest of the analyzed context carry the answer.
    return "";
  }
}

function extractSnippet(transcript, terms) {
  if (!transcript) return "";
  const lower = transcript.toLowerCase();

  // Filter to terms that are likely meaningful for anchoring (drops very short
  // or empty terms). Short words like "the", "did", "say", "ceo" tend to be
  // either stopwords or so common that they don't discriminate. We keep them
  // in the broader keyword-scoring layer (rankEpisodes), but exclude them here
  // when picking WHERE in the transcript to anchor the snippet.
  const meaningful = terms.filter((t) => t && t.length >= 4);
  const anchorTerms = meaningful.length > 0 ? meaningful : terms;

  // For each anchor term, collect ALL its occurrences in the transcript and
  // weight each by inverse frequency. A term that appears 5 times has weight
  // 0.2 per occurrence; a term that appears 500 times has weight 0.002. So
  // rare specific terms (like "Cloudflare") dominate over common ones (like
  // "the"), without needing an explicit stopword list.
  const positions = []; // [{pos, weight}]
  for (const t of anchorTerms) {
    if (!t) continue;
    const matches = [];
    let i = 0;
    while ((i = lower.indexOf(t, i)) !== -1) {
      matches.push(i);
      i += t.length;
      if (matches.length > 100) break; // cap to bound CPU cost
    }
    if (matches.length === 0) continue;
    const weight = 1 / matches.length;
    for (const pos of matches) positions.push({ pos, weight });
  }

  if (positions.length === 0) {
    // No matching anchor terms found - fall back to the start of the transcript
    return transcript.slice(0, SNIPPET_FALLBACK_CHARS);
  }

  // Pick the position with the highest density of weighted matches within a
  // RADIUS-character window. This finds where the rare query terms CLUSTER -
  // i.e. where the actual discussion of the topic lives, not just where the
  // single rarest term happens to appear once.
  const RADIUS = 1000;
  let bestPos = positions[0].pos;
  let bestScore = -1;
  for (const cand of positions) {
    let score = 0;
    for (const other of positions) {
      if (Math.abs(other.pos - cand.pos) <= RADIUS) {
        score += other.weight;
      }
    }
    if (score > bestScore) {
      bestScore = score;
      bestPos = cand.pos;
    }
  }

  const start = Math.max(0, bestPos - SNIPPET_BEFORE_CHARS);
  const end = Math.min(transcript.length, bestPos + SNIPPET_AFTER_CHARS);
  const prefix = start > 0 ? "..." : "";
  const suffix = end < transcript.length ? "..." : "";
  return prefix + transcript.slice(start, end) + suffix;
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}
