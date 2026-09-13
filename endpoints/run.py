"""Eval Loop endpoint.

Pulls a linked Dify app's real conversation logs - question/answer pairs,
any user feedback already attached to each message, and the actual
knowledge-base passages that were retrieved for each answer (Dify embeds
these in every message when retriever_resource is enabled, so no separate
knowledge-base access is needed) - and judges each one with an LLM for
groundedness, relevance, correctness, and reusability. Returns a structured
report; this does not create annotations or modify anything.
"""

import json
import logging
import re
import time
from typing import Any, Dict, List, Mapping, Optional

import requests
from dify_plugin import Endpoint
from dify_plugin.entities.model.message import SystemPromptMessage, UserPromptMessage
from werkzeug import Request, Response

logger = logging.getLogger(__name__)

BASE_EVAL_INSTRUCTION = """You are auditing real support-agent conversation logs for quality.

You may be given the actual knowledge-base passages that were retrieved and available when the answer was generated. If passages are provided, ground your judgment in them - this is the real source of truth, not general knowledge. If no passages are provided, judge plausibility and internal coherence instead.

Not every message needs a substantive, KB-grounded answer. A greeting, an off-topic request, or anything outside what this agent is meant to handle may correctly receive a brief acknowledgment, decline, or redirect instead - judge such a response on whether it's the *appropriate* reply to that message, not on whether it happens to cite a knowledge-base passage. A deliberate redirect isn't a factual claim, so don't mark it ungrounded just because no passage discusses the off-topic subject. Only genuine in-scope questions need to be checked for substantive, grounded correctness.

For the given question/answer pair, judge:
- "grounded": for a genuine in-scope question, whether the answer is actually supported by the retrieved passages, with no claims that go beyond or contradict them. For a greeting/off-topic/out-of-scope message where a brief decline or redirect is the appropriate reply, a correct redirect counts as grounded.
- "relevant": whether the answer is the *appropriate* response to what the user was asking or trying to accomplish - which can be a redirect or brief acknowledgment when that's the right call, not only a literal on-topic answer.
- "correct": your overall judgment of accuracy and appropriateness, combining groundedness and relevance.
- "reusable": whether this Q&A is generalizable knowledge that would help a different user asking something similar - not a one-off case tied to specific names, order IDs, or dates, and not a greeting/small-talk/redirect exchange.
- "issue": a short note describing what's wrong (empty string if nothing is wrong).

If real user feedback is provided and it's negative ("dislike"), treat that as a strong signal the answer may be wrong, and explain what likely went wrong.
"""

RESPONSE_FORMAT_INSTRUCTION = """
Respond with ONLY a raw JSON object, no markdown fences, no commentary:
{"grounded": true or false, "relevant": true or false, "correct": true or false, "reusable": true or false, "issue": "..."}
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
    """Thin client for the subset of Dify's Service API this endpoint needs.

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


class RunEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        app_id = settings.get("app", {}).get("app_id")
        base_url = settings.get("dify_base_url", "").rstrip("/")
        api_key = settings.get("dify_api_key")
        eval_model = settings.get("eval_model")
        target_users_raw = settings.get("target_users", "")

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
            return self._error(400, f"Missing required settings: {', '.join(missing)}")

        target_users = [u.strip() for u in target_users_raw.split(",") if u.strip()]
        if not target_users:
            return self._error(400, "'target_users' must list at least one Dify end-user identifier (comma-separated).")

        lookback_days = self._parse_int(settings.get("lookback_days"), default=1)
        max_messages = self._parse_int(settings.get("max_messages"), default=50)
        cutoff_ts = time.time() - lookback_days * 86400
        instruction = build_eval_instruction(settings.get("custom_instruction"))

        client = DifyApiClient(base_url, api_key)
        results: List[Dict[str, Any]] = []
        errors: List[str] = []

        for user in target_users:
            if len(results) >= max_messages:
                break

            try:
                conv_iter = iter(client.iter_conversations(user, cutoff_ts))
                while len(results) < max_messages:
                    conv = next(conv_iter)
                    self._collect_from_conversation(
                        client, conv, user, cutoff_ts, max_messages, eval_model, instruction, results, errors
                    )
            except StopIteration:
                pass
            except DifyApiError as e:
                errors.append(f"[{user}] failed to list conversations: {e}")
                continue

        return Response(
            status=200,
            response=json.dumps(
                {
                    "app_id": app_id,
                    "lookback_days": lookback_days,
                    "evaluated": len(results),
                    "results": results,
                    "errors": errors,
                },
                indent=2,
                ensure_ascii=False,
            ),
            content_type="application/json",
        )

    def _collect_from_conversation(
        self,
        client: "DifyApiClient",
        conv: Dict[str, Any],
        user: str,
        cutoff_ts: float,
        max_messages: int,
        eval_model: Mapping,
        instruction: str,
        results: List[Dict[str, Any]],
        errors: List[str],
    ) -> None:
        """Evaluates messages from one conversation into `results`, appending
        to `errors` instead of raising if the messages listing itself fails -
        `iter_messages` is a generator, so the failure can only surface once
        this loop actually pulls from it, not at the call site."""
        try:
            for msg in client.iter_messages(conv["id"], user, cutoff_ts):
                if len(results) >= max_messages:
                    return
                question = (msg.get("query") or "").strip()
                answer = (msg.get("answer") or "").strip()
                if not question or not answer:
                    continue

                feedback = (msg.get("feedback") or {}).get("rating")
                passages = [
                    res.get("content")
                    for res in (msg.get("retriever_resources") or [])
                    if res.get("content")
                ]
                verdict = self._evaluate(eval_model, instruction, question, answer, feedback, passages)

                results.append(
                    {
                        "user": user,
                        "conversation_id": conv["id"],
                        "message_id": msg["id"],
                        "created_at": msg.get("created_at"),
                        "question": question,
                        "answer": answer,
                        "feedback": feedback,
                        "retrieved_passage_count": len(passages),
                        "eval": verdict,
                    }
                )
        except DifyApiError as e:
            errors.append(f"[{user}] failed to list messages for conversation {conv['id']}: {e}")

    def _evaluate(
        self,
        model_config: Mapping,
        instruction: str,
        question: str,
        answer: str,
        feedback: Optional[str],
        passages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        user_content = f"Question: {question}\n\nAnswer: {answer}"
        if passages:
            joined = "\n\n---\n\n".join(passages)
            user_content += f"\n\nRetrieved knowledge-base passages available when this answer was generated:\n{joined}"
        else:
            user_content += "\n\n(No knowledge-base passages were retrieved for this answer.)"
        if feedback:
            user_content += f"\n\nUser feedback on this answer: {feedback}"

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
            verdict = json.loads(content)
            return {
                "grounded": bool(verdict.get("grounded")),
                "relevant": bool(verdict.get("relevant")),
                "correct": bool(verdict.get("correct")),
                "reusable": bool(verdict.get("reusable")),
                "issue": verdict.get("issue", ""),
            }
        except Exception as e:
            logger.warning("Eval failed for a message: %s", e)
            return {
                "grounded": None,
                "relevant": None,
                "correct": None,
                "reusable": None,
                "issue": f"eval error: {e}",
            }

    @staticmethod
    def _parse_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _error(status: int, message: str) -> Response:
        return Response(status=status, response=json.dumps({"error": message}), content_type="application/json")
