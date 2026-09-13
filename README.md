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
- [Support](#support)

### Why this exists

Dify's Service API only lets you pull conversations/messages scoped to one end user at a time, and there's no built-in way to systematically review whether an agent's real answers are actually correct. This plugin closes that gap: it walks the conversation history for the end users you specify, and for each Q&A pair asks an LLM to judge:

- **Grounded** - is the answer actually supported by the knowledge-base passages that were retrieved for it (Dify attaches these to every message when the app has retrieval enabled), or does it go beyond/contradict them?
- **Relevant** - does the answer address the question, or is it a deflection/non-answer?
- **Correct** - overall accuracy, combining groundedness with general reasoning.
- **Reusable** - is this a generalizable Q&A worth turning into permanent knowledge, or a one-off case tied to specific names/order IDs/dates?

Real user feedback (like/dislike), where already recorded on a message, is passed to the eval model as an additional signal and included in the report.

### Settings

| Setting | Required | Description |
| --- | --- | --- |
| App | Yes | The Dify chat app whose logs you want to evaluate. |
| Dify API Base URL | Yes | The base URL of the Dify instance hosting that app, e.g. `https://api.dify.ai/v1` or your self-hosted instance's `/v1` URL. |
| App API Key | Yes | The selected app's own Service API key. Used to call that app's `/conversations` and `/messages` endpoints directly - the plugin SDK's built-in invocation doesn't expose log access, only chat/completion/workflow calls. |
| Target End Users | Yes | Comma-separated Dify end-user identifiers to pull conversations for, e.g. `slack-C0123,slack-D0456`. Dify's API scopes conversation listing per end user, so there's no way to list "everyone" - you tell it who to check. |
| Eval Model | Yes | The model used to judge each Q&A pair. |
| Lookback Window (days) | No | How many days back to pull conversations from. Default: `1`. |
| Max Messages Per Run | No | Safety cap on how many messages to evaluate in one run. Default: `50`. |

### Install

To install this plugin, specify the following GitHub repository when selecting "Install Plugin":

<!-- TODO: replace with the actual GitHub repo URL before publishing -->
`https://github.com/<your-org>/<your-repo>`

### Setup

1. In the Dify app you want to evaluate, go to **API Access** and generate (or copy) a Service API key.
2. Note the app's `user` identifiers - whatever string your integration passes as `user` when invoking the app (e.g. a Slack bot passing `slack-{channel}`). If your integration doesn't set this explicitly, every conversation falls back to one shared default user, and you can pass that instead.
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
      "user": "slack-C0123",
      "conversation_id": "...",
      "message_id": "...",
      "created_at": 1735689600,
      "question": "...",
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
  "errors": []
}
```

`errors` lists any per-user or per-conversation failures (e.g. an invalid end-user identifier) that didn't stop the rest of the run.

### Support

- Source repository: https://github.com/fr3on/eval-loop
- Issues / questions: https://github.com/fr3on/eval-loop/issues
- Contact: [@fr3on](https://github.com/fr3on) on GitHub
