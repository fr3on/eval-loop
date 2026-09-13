## Eval Loop

**Version:** 0.0.1
**Type:** extension

Pulls a Dify chat app's real conversation logs and evaluates the Q&A for groundedness, relevance, correctness, and reusability - using the actual knowledge-base passages Dify already retrieved for each answer, plus any user feedback already recorded. Returns a structured report; it does not create annotations or modify anything.

### Contents

- [Why this exists](#why-this-exists)
- [Settings](#settings)
- [Install](#install)
- [Setup](#setup)
- [Triggering a run](#triggering-a-run)
- [Report format](#report-format)
- [Viewing results in Dify](#viewing-results-in-dify)
- [Support](#support)

### Why this exists

Dify's Service API only lets you pull conversations/messages scoped to one end user at a time, and there's no built-in way to systematically review whether an agent's real answers are actually correct. This plugin closes that gap: it walks the conversation history for the end users you specify, and for each message/answer pair asks an LLM to judge:

- **Grounded** - is the answer actually supported by the knowledge-base passages that were retrieved for it (Dify attaches these to every message when the app has retrieval enabled), or does it go beyond/contradict them? A deliberate decline/redirect on an out-of-scope message counts as grounded too, since it isn't making a factual claim that needs support.
- **Relevant** - is the answer the *appropriate* response to what the user was asking or trying to accomplish - which can be a redirect or brief acknowledgment when that's the right call, not only a literal on-topic answer.
- **Correct** - overall accuracy and appropriateness, combining groundedness and relevance.
- **Reusable** - is this a generalizable piece of knowledge worth turning into permanent support material, or a one-off case tied to specific names/order IDs/dates (or just a greeting/redirect, which is never reusable)?

The base criteria are domain-agnostic. If your app has its own scope-limiting policy (e.g. "always redirect off-topic questions"), describe it in the **Custom Instruction** setting so the eval judges against your app's actual intended behavior instead of guessing.

Real user feedback (like/dislike), where already recorded on a message, is passed to the eval model as an additional signal and included in the report.

### Settings

| Setting | Required | Description |
| --- | --- | --- |
| App | Yes | The Dify chat app whose logs you want to evaluate. |
| Dify API Base URL | Yes | The base URL of the Dify instance hosting that app, e.g. `https://api.dify.ai/v1` or your self-hosted instance's `/v1` URL. |
| App API Key | Yes | The selected app's own Service API key. Used to call that app's `/conversations` and `/messages` endpoints directly - the plugin SDK's built-in invocation doesn't expose log access, only chat/completion/workflow calls. |
| Target End Users | Yes | Comma-separated Dify end-user identifiers to pull conversations for, e.g. `user-123,user-456`. Dify's API scopes conversation listing per end user, so there's no way to list "everyone" - you tell it who to check. |
| Eval Model | Yes | The model used to judge each Q&A pair. |
| Lookback Window (days) | No | How many days back to pull conversations from. Default: `1`. |
| Max Messages Per Run | No | Safety cap on how many messages to evaluate in one run. Default: `50`. |
| Custom Instruction | No | Describe this app's expected behavior so the eval judges it correctly - e.g. "This agent should always redirect off-topic questions rather than answering them - don't penalize that as a failure." The base eval criteria (groundedness, relevance, correctness, reusability) are domain-agnostic; this fills in what "correct" actually means for your specific app. |
| Save Report to Knowledge Base | No | When on, each run's report is saved as a new Document in a Dify Knowledge Base, giving you a persistent, searchable history. Default: off. |
| Knowledge Base ID | Required if saving | The dataset ID to write each run's report into. |
| Knowledge Base API Key | Required if saving | The Knowledge Base's own Service API key (Knowledge Base → API Access) - Dataset endpoints use a separate key from the App API Key above. |

### Install

To install this plugin, specify the following GitHub repository when selecting "Install Plugin":

https://github.com/fr3on/eval-loop

### Setup

1. In the Dify app you want to evaluate, go to **API Access** and generate (or copy) a Service API key.
2. Note the app's `user` identifiers - whatever string is passed as `user` when the app is invoked (via the chat API, a workflow, or any client integration). If nothing sets this explicitly, every conversation falls back to one shared default user, and you can pass that instead.
3. Install this plugin, select the target app, and fill in the base URL, API key, and target end users.
4. Pick an Eval Model - any LLM configured in your workspace.

### Triggering a run

This plugin exposes a single `POST /run` endpoint reachable at its installed webhook URL - it does not run on a schedule by itself, since the Dify plugin platform has no built-in cron trigger for Endpoint-type plugins. Trigger it however fits your setup, e.g.:

- Manually, with `curl` or Postman, whenever you want a report.
- From a Dify Workflow app with a native **Schedule Trigger** node, using an HTTP Request node to call this endpoint on a cron schedule - this keeps everything inside Dify with no external infrastructure.
- From any external scheduler (cron, GitHub Actions, a cloud scheduler) that can make an HTTP POST.

### Report format

A successful run returns:

```json
{
  "app_id": "...",
  "lookback_days": 1,
  "evaluated": 12,
  "results": [
    {
      "user": "user-123",
      "conversation_id": "...",
      "message_id": "...",
      "created_at": 1735689600,
      "message": "...",
      "answer": "...",
      "feedback": "dislike",
      "retrieved_passage_count": 2,
      "eval": {
        "grounded": false,
        "relevant": true,
        "correct": false,
        "reusable": false,
        "issue": "Contradicts the retrieved passage, which says refunds ARE possible."
      }
    }
  ],
  "errors": [],
  "summary_markdown": "## Eval Loop Report\n\n- **Evaluated:** 12 message(s)\n- **Correct:** 10/12 (83%)\n..."
}
```

`message` is deliberately not called `question` - it's whatever the user typed (Dify's own field is called `query`), which is often a greeting, an acknowledgment, or small talk rather than a literal question.

`errors` lists any per-user or per-conversation failures (e.g. an invalid end-user identifier) that didn't stop the rest of the run.

`summary_markdown` is a ready-to-display Markdown report - counts plus a table of flagged (incorrect) messages with their issues - meant to be shown directly rather than parsed. See below for where to actually see it.

### Viewing results in Dify

This plugin doesn't keep its own run history by default - each run's report only exists in the `/run` response, unless something stores or displays it. Two ways to see results inside Dify:

**Option A - via a Workflow's own Logs (no extra setup)**

1. Build a Dify **Workflow** app with a native **Schedule Trigger** node (see [Triggering a run](#triggering-a-run)).
2. Add an **HTTP Request** node that calls this plugin's `/run` endpoint.
3. Add an **End** node whose output is the HTTP Request node's `summary_markdown` field.

Every scheduled run then shows up in that Workflow app's own **Logs** tab in Dify Studio, with the readable summary as the run's output. If you need the full structured `results` array (e.g. to feed a later automation step), reference that field from the same HTTP Request node's response instead.

**Option B - persistent history via a Knowledge Base**

Turn on **Save Report to Knowledge Base** (with a **Knowledge Base ID** and its own **API Key**) and every run writes its `summary_markdown` as a new Document into that dataset automatically - no Workflow needed. This gives you an actual searchable history across runs (visible in that Knowledge Base's Documents list in Dify Studio), and since it's a real Dify dataset, another app can even use it as RAG context - e.g. an admin-facing "ask about this app's quality trends" chatbot.

Both options can be used together.

### Support

- Source repository: https://github.com/fr3on/eval-loop
- Issues / questions: https://github.com/fr3on/eval-loop/issues
- Contact: [@fr3on](https://github.com/fr3on) on GitHub
