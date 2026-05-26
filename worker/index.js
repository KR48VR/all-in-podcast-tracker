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
const SNIPPET_TOP_N = 3;
const SNIPPET_BEFORE_CHARS = 600;
const SNIPPET_AFTER_CHARS = 1400;
const SNIPPET_FALLBACK_CHARS = 2000; // when no query term matched in transcript

const RAW_EPISODE_BASE =
  "https://raw.githubusercontent.com/KR48VR/all-in-podcast-tracker/main/data/episodes";

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
    const terms = parseTerms(retrievalQuery);
    const topForSnippets = relevant.slice(0, SNIPPET_TOP_N);
    const snippets = await Promise.all(
      topForSnippets.map((ep) => fetchTranscriptSnippet(ep.id, terms))
    );
    topForSnippets.forEach((ep, i) => { ep._snippet = snippets[i]; });

    const context = relevant.map(formatEpisode).join("\n\n---\n\n");
    const citations = relevant.map((e) => `${e.date} · ${e.title}`);

    const systemPrompt =
      "You are an expert analyst of the All-In Podcast. Answer the user's " +
      "question using the episode notes below. Quote the hosts' views where " +
      "relevant. If the notes don't contain the answer, say so plainly. " +
      "Some entries include a TRANSCRIPT_EXCERPT field with verbatim text " +
      "from the recording. When the user asks what someone said or how " +
      "someone framed something, the TRANSCRIPT_EXCERPT is your primary " +
      "source - quote from it directly. Transcripts don't have explicit " +
      "speaker labels, so infer the likely speaker from style and context " +
      "and qualify your attribution (e.g. 'likely Chamath, based on phrasing'). " +
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

async function fetchTranscriptSnippet(epId, terms) {
  // Fetch the raw per-episode JSON from GitHub, with edge cache.
  // We cache aggressively because episode JSONs only change on weekly workflow
  // runs - so a 1-hour cache is comfortably safe.
  if (!epId) return "";
  const url = `${RAW_EPISODE_BASE}/${epId}.json`;
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
  let bestIdx = -1;
  for (const t of terms) {
    if (!t) continue;
    const i = lower.indexOf(t);
    if (i >= 0 && (bestIdx < 0 || i < bestIdx)) bestIdx = i;
  }
  if (bestIdx < 0) {
    // No matching term found - fall back to the start of the transcript
    return transcript.slice(0, SNIPPET_FALLBACK_CHARS);
  }
  const start = Math.max(0, bestIdx - SNIPPET_BEFORE_CHARS);
  const end = Math.min(transcript.length, bestIdx + SNIPPET_AFTER_CHARS);
  // Add ellipses to make it clear this is an excerpt
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
