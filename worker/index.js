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
    if (!question) return json({ error: "missing question" }, 400);
    if (!env.GROQ_API_KEY) return json({ error: "GROQ_API_KEY not set" }, 500);

    const relevant = rankEpisodes(question, episodes).slice(0, TOP_K);
    const context = relevant.map(formatEpisode).join("\n\n---\n\n");
    const citations = relevant.map((e) => `${e.date} · ${e.title}`);

    const systemPrompt =
      "You are an expert analyst of the All-In Podcast. Answer the user's " +
      "question using ONLY the episode notes below. Quote the hosts' views " +
      "where relevant. If the notes don't contain the answer, say so plainly. " +
      "Keep answers tight and cite episode titles inline like (E. Title, YYYY-MM-DD). " +
      "When a takeaway or quote has a timestamp and youtube URL, include a " +
      "Markdown deep-link in the form [▶ mm:ss](youtube_url?t=SECONDSs) right " +
      "after the relevant sentence so the reader can jump to that moment.";

    const payload = {
      model: MODEL,
      messages: [
        { role: "system", content: systemPrompt },
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

function rankEpisodes(question, eps) {
  // Simple keyword score — cheap, deterministic, works well enough as a
  // retrieval step when the corpus is a few hundred summaries. Upgrade to
  // embeddings later if you want smarter retrieval.
  const terms = question
    .toLowerCase()
    .replace(/[^\w\s]/g, " ")
    .split(/\s+/)
    .filter((t) => t.length > 2);

  return eps
    .map((ep) => {
      // Takeaways may be strings (legacy) or {text, timestamp_seconds}.
      const takeawayText = (ep.takeaways || [])
        .map((t) => (typeof t === "string" ? t : t.text || ""))
        .join(" ");
      const hay = [
        ep.title, ep.summary,
        (ep.topics || []).join(" "),
        takeawayText,
        (ep.guests || []).join(" "),
      ].join(" ").toLowerCase();
      let score = 0;
      for (const t of terms) if (hay.includes(t)) score += 1;
      // Boost recent episodes slightly so "latest" queries feel right.
      if (ep.date) {
        const daysOld = (Date.now() - new Date(ep.date).getTime()) / 86_400_000;
        score += Math.max(0, 1 - daysOld / 365);
      }
      return { ...ep, _score: score };
    })
    .sort((a, b) => b._score - a._score);
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
    return `- "${q.text || ""}" — ${q.speaker || "Unknown"}${ts}`;
  };
  const lines = [
    `EPISODE: ${ep.title}`,
    `DATE: ${ep.date}`,
    ep.youtube_url ? `YOUTUBE_URL: ${ep.youtube_url}` : "",
    ep.guests?.length ? `GUESTS: ${ep.guests.join(", ")}` : "",
    ep.topics?.length ? `TOPICS: ${ep.topics.join(", ")}` : "",
    "",
    `SUMMARY: ${ep.summary || ""}`,
    "",
    "TAKEAWAYS:",
    ...(ep.takeaways || []).map(fmtTake),
    (ep.quotes || []).length ? "\nQUOTES:" : "",
    ...(ep.quotes || []).map(fmtQuote),
  ].filter(Boolean);
  return lines.join("\n");
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
