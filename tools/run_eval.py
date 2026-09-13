"""Run Eval tool.

Pulls a linked Dify app's real conversation logs - message/answer pairs,
any user feedback already attached to each message, and the actual
knowledge-base passages that were retrieved for each answer (Dify embeds
these in every message when retriever_resource is enabled, so no separate
knowledge-base access is needed) - and judges each one with an LLM for
groundedness, relevance, correctness, and reusability. Returns a structured
report, and optionally saves it as a Document in a Dify Knowledge Base if
configured. This never creates annotations or otherwise modifies the
evaluated app.
"""

import json
import logging
import re
import time
from typing import Any, Dict, Generator, List, Mapping, Optional

import requests
from dify_plugin import Tool
from dify_plugin.entities.model.message import SystemPromptMessage, UserPromptMessage
from dify_plugin.entities.tool import ToolInvokeMessage

logger = logging.getLogger(__name__)

BASE_EVAL_INSTRUCTION = """You are auditing real support-agent conversation logs for quality.

You may be given the actual knowledge-base passages that were retrieved and available when each answer was generated. If passages are provided for a pair, ground your judgment in them - this is the real source of truth, not general knowledge. If no passages are provided, judge plausibility and internal coherence instead.

Not every message needs a substantive, KB-grounded answer. A greeting, an off-topic request, or anything outside what this agent is meant to handle may correctly receive a brief acknowledgment, decline, or redirect instead - judge such a response on whether it's the *appropriate* reply to that message, not on whether it happens to cite a knowledge-base passage. A deliberate redirect isn't a factual claim, so don't mark it ungrounded just because no passage discusses the off-topic subject. Only genuine in-scope questions need to be checked for substantive, grounded correctness.

For each message/answer pair, judge:
- "grounded": for a genuine in-scope question, whether the answer is actually supported by the retrieved passages, with no claims that go beyond or contradict them. For a greeting/off-topic/out-of-scope message where a brief decline or redirect is the appropriate reply, a correct redirect counts as grounded.
- "relevant": whether the answer is the *appropriate* response to what the user was asking or trying to accomplish - which can be a redirect or brief acknowledgment when that's the right call, not only a literal on-topic answer.
- "correct": your overall judgment of accuracy and appropriateness, combining groundedness and relevance.
- "reusable": whether this Q&A is generalizable knowledge that would help a different user asking something similar - not a one-off case tied to specific names, order IDs, or dates, and not a greeting/small-talk/redirect exchange.
- "issue": a short note describing what's wrong (empty string if nothing is wrong).
- "corrected_answer": ONLY when "correct" is false, write what the answer should have said instead - grounded in the retrieved passages if provided, otherwise your best correction. Leave as an empty string when "correct" is true. Write it as a complete, standalone answer, not a diff or a note about what was wrong.

If real user feedback is provided for a pair and it's negative ("dislike"), treat that as a strong signal the answer may be wrong, and explain what likely went wrong.
"""

RESPONSE_FORMAT_INSTRUCTION = """
You will be given a numbered list of message/answer pairs to judge in this single request.

Respond with ONLY a raw JSON array, no markdown fences, no commentary, with exactly one object per pair, IN THE SAME ORDER as given:
[{"grounded": true or false, "relevant": true or false, "correct": true or false, "reusable": true or false, "issue": "...", "corrected_answer": "..."}, ...]
"""


def build_eval_instruction(custom_instruction: Optional[str]) -> str:
    """Combines the base, app-agnostic eval criteria with optional
    operator-supplied guidance about this specific app's expected behavior
    (e.g. "this agent should always redirect off-topic questions rather than
    answering them - don't penalize that as a failure")."""
    parts = [BASE_EVAL_INSTRUCTION]
    if custom_instruction:
        parts.append(f"Additional guidance specific to this app, from its operator:\n{custom_instruction}\n")
    parts.append(RESPONSE_FORMAT_INSTRUCTION)
    return "\n".join(parts)


class DifyApiError(Exception):
    pass


class DifyApiClient:
    """Thin client for the subset of Dify's Service API this tool needs.

    Deliberately not using the plugin SDK's backward-invocation here: it only
    exposes chat/completion/workflow/fetch_app, none of which cover pulling
    conversations, messages, or feedback - those require calling the app's
    own REST API directly with its Service API key.
    """

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = requests.get(f"{self.base_url}{path}", headers=self.headers, params=params, timeout=30)
        if resp.status_code != 200:
            raise DifyApiError(f"{path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # Hard cap on pages scanned per conversations/messages listing, so a very
    # long history can't cause unbounded API calls when most of it predates
    # the cutoff (see the sort-order note below).
    _MAX_SCAN_PAGES = 50

    def iter_conversations(self, user: str, cutoff_ts: float):
        """Yields conversations for `user` created at or after `cutoff_ts`.

        Dify sorts conversations by `updated_at` descending by default, not
        `created_at` - an old conversation that just got a new reply sorts
        near the top despite an old created_at. Stopping at the first
        created_at-older-than-cutoff item (as an earlier version of this did)
        would then silently skip every conversation after it, including
        genuinely recent ones that simply hadn't been touched as recently.
        So this skips old items instead of stopping, and only ends once the
        API reports no more pages or the scan cap is hit.
        """
        last_id = None
        for _ in range(self._MAX_SCAN_PAGES):
            params: Dict[str, Any] = {"user": user, "limit": 100}
            if last_id:
                params["last_id"] = last_id
            page = self._get("/conversations", params)
            data = page.get("data", [])
            if not data:
                return
            for conv in data:
                if (conv.get("created_at") or 0) >= cutoff_ts:
                    yield conv
            if not page.get("has_more"):
                return
            last_id = data[-1]["id"]

    def iter_messages(self, conversation_id: str, user: str, cutoff_ts: float):
        """Yields messages for a conversation created at or after `cutoff_ts`.
        Messages have no separate updated_at, so this is mainly for symmetry
        and defense in depth - see iter_conversations for why "skip, don't
        stop" matters."""
        first_id = None
        for _ in range(self._MAX_SCAN_PAGES):
            params: Dict[str, Any] = {"conversation_id": conversation_id, "user": user, "limit": 100}
            if first_id:
                params["first_id"] = first_id
            page = self._get("/messages", params)
            data = page.get("data", [])
            if not data:
                return
            for msg in data:
                if (msg.get("created_at") or 0) >= cutoff_ts:
                    yield msg
            if not page.get("has_more"):
                return
            first_id = data[0]["id"]


class RunEvalTool(Tool):
    def _invoke(self, tool_parameters: Mapping[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        credentials = self.runtime.credentials

        app_id = (credentials.get("app") or {}).get("app_id")
        base_url = (credentials.get("dify_base_url") or "").rstrip("/")
        api_key = credentials.get("dify_api_key")
        eval_model = credentials.get("eval_model")
        target_users_raw = tool_parameters.get("target_users", "")

        missing = [
            name
            for name, value in (
                ("app", app_id),
                ("dify_base_url", base_url),
                ("dify_api_key", api_key),
                ("eval_model", eval_model),
                ("target_users", target_users_raw),
            )
            if not value
        ]
        if missing:
            yield self.create_text_message(f"Missing required settings: {', '.join(missing)}")
            return

        target_users = [u.strip() for u in target_users_raw.split(",") if u.strip()]
        if not target_users:
            yield self.create_text_message(
                "'Target End Users' must list at least one Dify end-user identifier (comma-separated)."
            )
            return

        lookback_days = self._parse_int(tool_parameters.get("lookback_days"), default=1)
        max_messages = self._parse_int(tool_parameters.get("max_messages"), default=50)
        eval_batch_size = max(1, self._parse_int(tool_parameters.get("eval_batch_size"), default=5))
        cutoff_ts = time.time() - lookback_days * 86400
        instruction = build_eval_instruction(tool_parameters.get("custom_instruction"))

        client = DifyApiClient(base_url, api_key)
        candidates: List[Dict[str, Any]] = []
        errors: List[str] = []

        # Phase 1: pull message/answer candidates from Dify's logs, without
        # evaluating them yet.
        for user in target_users:
            if len(candidates) >= max_messages:
                break

            try:
                conv_iter = iter(client.iter_conversations(user, cutoff_ts))
                while len(candidates) < max_messages:
                    conv = next(conv_iter)
                    self._collect_from_conversation(client, conv, user, cutoff_ts, max_messages, candidates, errors)
            except StopIteration:
                pass
            except DifyApiError as e:
                errors.append(f"[{user}] failed to list conversations: {e}")
                continue

        # Phase 2: evaluate the candidates in batches, so a single eval model
        # call judges several pairs at once instead of one call per message.
        results: List[Dict[str, Any]] = []
        for i in range(0, len(candidates), eval_batch_size):
            batch = candidates[i : i + eval_batch_size]
            verdicts = self._evaluate_batch(eval_model, instruction, batch)
            for item, verdict in zip(batch, verdicts):
                entry = {k: v for k, v in item.items() if k != "passages"}
                entry["eval"] = verdict
                results.append(entry)

        # Phase 3: derive DPO-style preference pairs from flagged answers -
        # (prompt, chosen, rejected) triples ready for preference-based
        # fine-tuning (e.g. HuggingFace TRL's DPOTrainer format). "chosen" is
        # the eval model's corrected_answer, grounded in the same retrieved
        # passages it judged against; "rejected" is the actual flagged answer.
        dpo_pairs = [
            {"prompt": r["message"], "chosen": r["eval"]["corrected_answer"], "rejected": r["answer"]}
            for r in results
            if r["eval"].get("correct") is False and r["eval"].get("corrected_answer")
        ]

        report: Dict[str, Any] = {
            "app_id": app_id,
            "lookback_days": lookback_days,
            "evaluated": len(results),
            "results": results,
            "dpo_pairs": dpo_pairs,
            "errors": errors,
            "summary_markdown": self._build_summary_markdown(results, dpo_pairs, errors),
        }

        if credentials.get("save_to_dataset", False):
            dataset_id = credentials.get("dataset_id")
            dataset_api_key = credentials.get("dataset_api_key")
            if not dataset_id or not dataset_api_key:
                report["saved_to_dataset"] = {
                    "saved": False,
                    "error": "'Save Report to Knowledge Base' is on, but Knowledge Base ID/API Key is missing.",
                }
            else:
                report["saved_to_dataset"] = self._save_report_to_dataset(
                    base_url, dataset_id, dataset_api_key, report["summary_markdown"]
                )

        yield self.create_json_message(report)
        yield self.create_variable_message("summary_markdown", report["summary_markdown"])
        yield self.create_variable_message("evaluated", report["evaluated"])
        yield self.create_variable_message("dpo_pairs", report["dpo_pairs"])
        yield self.create_variable_message("errors", report["errors"])
        yield self.create_text_message(report["summary_markdown"])

    def _save_report_to_dataset(
        self, base_url: str, dataset_id: str, dataset_api_key: str, summary_markdown: str
    ) -> Dict[str, Any]:
        """Saves the report as a new Document in a Dify Knowledge Base, using
        the dataset's own API key - Dataset endpoints are authenticated
        separately from App endpoints, so this is deliberately not reusing
        the App API Key setting. Failure here doesn't affect the eval report
        itself, which has already been computed by this point."""
        try:
            resp = requests.post(
                f"{base_url}/datasets/{dataset_id}/document/create-by-text",
                headers={"Authorization": f"Bearer {dataset_api_key}", "Content-Type": "application/json"},
                json={
                    "name": f"Eval Loop Report - {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
                    "text": summary_markdown,
                    "indexing_technique": "high_quality",
                },
                timeout=30,
            )
        except Exception as e:
            return {"saved": False, "error": str(e)}

        if resp.status_code not in (200, 201):
            return {"saved": False, "error": f"{resp.status_code}: {resp.text[:300]}"}

        data = resp.json()
        return {
            "saved": True,
            "document_id": (data.get("document") or {}).get("id"),
            "batch": data.get("batch"),
        }

    def _collect_from_conversation(
        self,
        client: "DifyApiClient",
        conv: Dict[str, Any],
        user: str,
        cutoff_ts: float,
        max_messages: int,
        candidates: List[Dict[str, Any]],
        errors: List[str],
    ) -> None:
        """Appends message/answer candidates from one conversation into
        `candidates`, without evaluating them yet (that happens in a later,
        batched phase). Appends to `errors` instead of raising if the
        messages listing itself fails - `iter_messages` is a generator, so
        the failure can only surface once this loop actually pulls from it,
        not at the call site."""
        try:
            for msg in client.iter_messages(conv["id"], user, cutoff_ts):
                if len(candidates) >= max_messages:
                    return
                # Dify's own field is called "query" - it's whatever the user
                # typed, not necessarily a literal question (could be a
                # greeting, an acknowledgment, small talk, etc.). Keep that
                # framing rather than mislabeling every turn as a "question".
                message = (msg.get("query") or "").strip()
                answer = (msg.get("answer") or "").strip()
                if not message or not answer:
                    continue

                feedback = (msg.get("feedback") or {}).get("rating")
                passages = [
                    res.get("content")
                    for res in (msg.get("retriever_resources") or [])
                    if res.get("content")
                ]

                candidates.append(
                    {
                        "user": user,
                        "conversation_id": conv["id"],
                        "message_id": msg["id"],
                        "created_at": msg.get("created_at"),
                        "message": message,
                        "answer": answer,
                        "feedback": feedback,
                        "retrieved_passage_count": len(passages),
                        "passages": passages,
                    }
                )
        except DifyApiError as e:
            errors.append(f"[{user}] failed to list messages for conversation {conv['id']}: {e}")

    @staticmethod
    def _build_batch_user_content(batch: List[Dict[str, Any]]) -> str:
        blocks = []
        for i, item in enumerate(batch, start=1):
            block = f"--- Pair {i} ---\nUser message: {item['message']}\n\nAgent's answer: {item['answer']}"
            passages = item.get("passages")
            if passages:
                joined = "\n\n---\n\n".join(passages)
                block += f"\n\nRetrieved knowledge-base passages available when this answer was generated:\n{joined}"
            else:
                block += "\n\n(No knowledge-base passages were retrieved for this answer.)"
            feedback = item.get("feedback")
            if feedback:
                block += f"\n\nUser feedback on this answer: {feedback}"
            blocks.append(block)
        return "\n\n".join(blocks)

    def _evaluate_batch(
        self, model_config: Mapping, instruction: str, batch: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Judges an entire batch of message/answer pairs in a single LLM
        call. On any failure - a bad response, a length mismatch, anything -
        the whole batch falls back to per-item error verdicts, since we can't
        reliably tell which items in a malformed response corresponded to
        which input."""
        if not batch:
            return []

        user_content = self._build_batch_user_content(batch)
        try:
            result = self.session.model.llm.invoke(
                model_config=dict(model_config),
                prompt_messages=[
                    SystemPromptMessage(content=instruction),
                    UserPromptMessage(content=user_content),
                ],
                stream=False,
            )
            content = (result.message.content or "").strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE).strip()
            parsed = json.loads(content)
            if not isinstance(parsed, list) or len(parsed) != len(batch):
                got = f"{type(parsed).__name__} of length {len(parsed)}" if isinstance(parsed, list) else type(parsed).__name__
                raise ValueError(f"expected a JSON array of {len(batch)} verdict(s), got {got}")
            return [self._normalize_verdict(v) for v in parsed]
        except Exception as e:
            logger.warning("Batch eval failed for %d item(s): %s", len(batch), e)
            return [self._error_verdict(str(e)) for _ in batch]

    @staticmethod
    def _normalize_verdict(verdict: Mapping) -> Dict[str, Any]:
        return {
            "grounded": bool(verdict.get("grounded")),
            "relevant": bool(verdict.get("relevant")),
            "correct": bool(verdict.get("correct")),
            "reusable": bool(verdict.get("reusable")),
            "issue": verdict.get("issue", ""),
            "corrected_answer": verdict.get("corrected_answer") or "",
        }

    @staticmethod
    def _error_verdict(error_msg: str) -> Dict[str, Any]:
        return {
            "grounded": None,
            "relevant": None,
            "correct": None,
            "reusable": None,
            "issue": f"eval error: {error_msg}",
            "corrected_answer": "",
        }

    @staticmethod
    def _build_summary_markdown(
        results: List[Dict[str, Any]], dpo_pairs: List[Dict[str, Any]], errors: List[str]
    ) -> str:
        """A human-readable summary, meant to be displayed directly (e.g. as
        a Dify Workflow node's output) instead of the raw JSON report."""
        total = len(results)
        if total == 0:
            return "## Eval Loop Report\n\nNo messages found in the configured window." + (
                f"\n\n**Run errors:**\n" + "\n".join(f"- {e}" for e in errors) if errors else ""
            )

        def count(field: str, value: Any) -> int:
            return sum(1 for r in results if r["eval"].get(field) is value)

        correct = count("correct", True)
        grounded = count("grounded", True)
        reusable = [r for r in results if r["eval"].get("reusable") is True]
        flagged = [r for r in results if r["eval"].get("correct") is False]
        eval_errors = [r for r in results if r["eval"].get("correct") is None]

        def cell(text: str, limit: int = 80) -> str:
            text = (text or "").replace("|", "\\|").replace("\n", " ")
            return text if len(text) <= limit else text[: limit - 1] + "…"

        lines = [
            "## Eval Loop Report",
            "",
            f"- **Evaluated:** {total} message(s)",
            f"- **Correct:** {correct}/{total} ({correct * 100 // total}%)",
            f"- **Grounded:** {grounded}/{total}",
            f"- **Reusable candidates:** {len(reusable)}",
            f"- **DPO pairs generated:** {len(dpo_pairs)}",
            f"- **Eval errors:** {len(eval_errors)}",
            f"- **Run errors:** {len(errors)}",
        ]

        if flagged:
            lines += ["", "### ⚠️ Flagged (incorrect)", "", "| User | Message | Answer | Issue |", "| --- | --- | --- | --- |"]
            for r in flagged:
                lines.append(
                    f"| {cell(r['user'], 20)} | {cell(r['message'])} | {cell(r['answer'])} | {cell(r['eval'].get('issue', ''))} |"
                )

        if errors:
            lines += ["", "### Run errors", ""] + [f"- {e}" for e in errors]

        return "\n".join(lines)

    @staticmethod
    def _parse_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
