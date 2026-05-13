# How the AI Readiness Analysis Works

A plain-language guide to the scores, frameworks, and narratives you see on the course dashboards.

---

## The Big Picture

When you process a course, the system pulls every piece of content — pages, assignments, quizzes, and discussions — from Canvas and runs each one through a three-stage AI pipeline:

1. **Extract** — Gather all course content from a Canvas export.
2. **Evaluate** — An AI scores every item across 8 pedagogical dimensions (this is what powers the dashboards).
3. **Transform** — The AI rewrites each item to be more engaging, AI-resilient, and pedagogically stronger, guided by the scores from step 2.

This document focuses on **step 2 (Evaluate)** and how those scores feed into the dashboards you see on the `/history` page.

---

## The 8 Dimensions the AI Scores

Every course item is sent to a large language model (LLM) — think of it as a specialized AI tutor — with a detailed rubric. The AI reads the actual content (not just keywords) and returns a structured score on each of these dimensions:

### 1. Bloom's Taxonomy Level (1–6)

Bloom's Taxonomy is a widely-used educational framework that classifies thinking skills from simple to complex:

| Level | Label        | What It Means                                      |
|------:|--------------|----------------------------------------------------|
| 1     | **Remember** | Recall facts, definitions, or formulas              |
| 2     | **Understand** | Explain ideas, summarize concepts                 |
| 3     | **Apply**    | Use knowledge in a new situation                    |
| 4     | **Analyze**  | Break information into parts, find patterns         |
| 5     | **Evaluate** | Justify a decision, critique an argument            |
| 6     | **Create**   | Design something new, synthesize original work      |

**Why it matters:** Content stuck at levels 1–2 (Remember/Understand) is easy for AI to complete on a student's behalf. Higher levels require genuine thinking.

### 2. AIAS Level — AI Integration Assessment Scale (1–5)

This custom scale rates how the course content envisions (or could envision) AI's role:

| Level | Label                 | What It Means                                    |
|------:|-----------------------|--------------------------------------------------|
| 1     | **No AI**             | AI plays no role                                  |
| 2     | **AI as Info Source**  | Students might use AI to look things up           |
| 3     | **AI as Tutor**       | AI could coach or explain to students             |
| 4     | **AI as Collaborator** | Students work *with* AI as a thinking partner    |
| 5     | **AI as Co-creator**  | AI is a genuine creative partner in the work      |

**Why it matters:** It shows how thoughtfully (or not) AI has been integrated into the learning design.

### 3. AI Leverage Potential (1–10)

How transformatively AI could **help** students learn this material. A high score means AI could be an outstanding learning partner (tutoring, practice, feedback). A low score means AI adds little value to the learning experience.

### 4. Cheating Vulnerability (1–10)

How easily a student could fully delegate this work to AI and receive full credit — **the key risk metric**. A score of 10 means a student could paste the assignment into ChatGPT and turn in the result with no personal effort.

### 5. Authenticity Score (1–10)

How much the work requires unique personal knowledge, experience, or context that an AI cannot fabricate. High-authenticity assignments ask students to draw on their own lives, local data, interviews, or professional experience.

### 6. Pedagogical Quality (1–10)

Overall instructional design quality of the *original* content: Is it well-structured? Are expectations clear? Are examples provided?

### 7. Transparency Score (1–10)

How clearly the assignment communicates expectations, success criteria, and grading rubrics to students. A score of 10 means everything is crystal clear.

### 8. Alignment Score (1–10)

How well the assessment actually measures its stated learning objectives. A high score means every task directly connects to what students are supposed to learn.

---

## How the Overall "AI Readiness Score" Is Calculated

The headline number you see on the dashboard — the **AI Readiness Score** — is a weighted composite of four of the eight dimensions:

```
AI Readiness = (AI Leverage × 0.25)
             + ((10 − Cheating Vulnerability) × 0.35)
             + (Authenticity × 0.25)
             + (Pedagogical Quality × 0.15)
```

### Why these weights?

- **Cheating Vulnerability gets the highest weight (35%)** because it represents the most urgent risk in the AI era — and it's *inverted* (subtracted from 10) so that *lower* vulnerability contributes *more* to readiness.
- **AI Leverage (25%)** rewards content that can productively use AI as a learning tool.
- **Authenticity (25%)** rewards work that requires genuine personal engagement.
- **Pedagogical Quality (15%)** ensures baseline instructional soundness.

### Readiness Labels

The final score maps to a human-readable label:

| Score Range | Label         |
|-------------|---------------|
| 8.0–10.0    | **Excellent** |
| 6.5–7.9     | **Good**      |
| 5.0–6.4     | **Moderate**  |
| 3.5–4.9     | **Fair**      |
| 0.0–3.4     | **Needs Work**|

---

## How the AI Actually Evaluates Each Item

The system doesn't use keyword matching or simple rules. Here's what happens behind the scenes:

1. **Content extraction** — The system pulls the title and body text from each Canvas item (stripping HTML formatting). Pages get up to 4,000 characters of body text; assignments get up to 5,000 characters of description; quizzes include the first 20 questions; discussions include the prompt message.

2. **Filtering** — Items that are clearly administrative (e.g., "KEEP HIDDEN," "INSTRUCTORS ONLY," "[INTERNAL]") or have very little content (under 40 characters) are automatically flagged as non-instructional and skipped.

3. **LLM prompt** — Each remaining item is sent to the AI with:
   - The course name for context
   - The item type (page, assignment, quiz, discussion)
   - The content preview
   - A detailed scoring rubric explaining each dimension
   - Instructions to return a structured JSON response

4. **Structured output** — The AI returns scores for all 8 dimensions plus:
   - **Key strengths** — what the content does well
   - **Key concerns** — where it falls short
   - **Priority recommendations** — specific, actionable suggestions for improvement

5. **Parallel processing** — Multiple items are evaluated simultaneously (configurable, default 3 at a time) for speed.

6. **Fallback** — If the AI fails on a particular item (timeout, parsing error), the system assigns neutral default scores (5.0 across the board) so the pipeline isn't blocked.

---

## What Powers the Dashboard Visualizations

The `/history` page gives you access to three reports for each processed course:

### 📊 AI Dashboard

The visual dashboard shows:
- **Summary cards** — Total items, average AI leverage, average cheating risk, overall AI readiness score
- **Content type breakdown** — Average scores split by assignments, quizzes, pages, and discussions
- **Risk vs. Leverage scatter plot** — An SVG chart plotting every item by its AI leverage (x-axis) vs. cheating risk (y-axis). Items in the upper-right corner are high leverage *and* high risk — the danger zone.
- **Due date timeline** — Upcoming assignments sorted by date
- **Full item table** — Every item with leverage, risk, readiness, and Bloom's level, filterable by content type

### 📈 AI Readiness Report

A deeper dive that includes:
- **Course-level metric cards** with bar charts for AI Leverage, Cheating Vulnerability, Authenticity, and Pedagogical Quality
- **Breakdown by content type** — Average readiness, vulnerability, and Bloom's level per type
- **High-risk flags** — Every item scoring ≥ 7.0 on cheating vulnerability, with its top recommendations
- **Prioritized recommendations** — The top 12 actionable suggestions across the course, pulled from the highest-risk items first
- **Expandable per-item analysis** — Click any item to see all its scores, strengths, concerns, and recommendations

### 🔍 Changes Report

After the AI enhances content (step 3), this report shows:
- **Side-by-side comparison** of original Canvas content vs. AI-enhanced version
- **Word-level inline diff** highlighting what was added (green) and removed (red)
- **Evaluation badges** on each item showing readiness score, vulnerability, and Bloom's level

---

## How the AI Enhancement (Transform) Uses These Scores

The scores from step 2 aren't just for reporting — they directly guide how the AI rewrites content in step 3:

- **High cheating vulnerability (≥ 7)?** The enhancement prompt includes explicit instructions to redesign for authentic, non-delegatable work — requiring personal engagement, real-world data, or documented process artifacts.
- **Low Bloom's level (≤ 2)?** The AI is told to push the content toward Analyze, Evaluate, and Create levels.
- **Low authenticity (≤ 4)?** The AI adds personal context requirements, unique data collection, or documented process elements.
- **Low transparency (≤ 4)?** The AI adds explicit rubrics, success criteria, and worked examples.
- **Weak alignment (≤ 4)?** The AI ensures every task directly assesses the stated objectives.

Each content type gets a specialized enhancement prompt:
- **Pages** get learning objectives, key takeaways, and reflection questions
- **Assignments** get grading rubrics, AI policies (when vulnerability is high), and metacognitive reflection components
- **Quizzes** get study guides, preparation strategies, and question analysis showing how to upgrade recall questions to higher-order thinking
- **Discussions** get scaffolding questions, response requirements, and exemplar descriptions

---

## The AI Models Used

The system supports multiple AI providers:
- **Anthropic** (Claude Haiku 4.5) — default via NebulaOne
- **OpenAI** (GPT-4o Mini)
- **Google Gemini** (Gemini 2.5 Flash)

All calls include automatic retry with exponential back-off (1 second, then 2 seconds) to handle temporary API issues gracefully.

---

## In Summary

The system reads your actual course content, uses established educational frameworks (Bloom's Taxonomy, AI integration scales), and applies AI analysis — not simple keyword matching — to produce actionable scores and recommendations. The goal is to help instructors understand where their course stands in terms of AI readiness and get specific, practical guidance on what to improve.

Every score you see on the dashboard traces back to an AI evaluation of the real content, weighted by pedagogical research priorities, and displayed through interactive visualizations designed to make the data immediately useful.
