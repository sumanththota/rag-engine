"""RAG orchestrator: ingest + retrieve + prompt build + rewrite.

Ported from internal/rag/service.go and internal/rag/rewrite.go.
"""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ValidationError

from app.chunk import Chunk, ChunkInput
from app.chunk import build as chunk_build
from app.embed import OllamaClient
from app.llm import ChatMessage, OpenAICompatibleClient
from app.pdfextract import extract_by_page
from app.store import PostgresStore, SearchResult

ingest_logger = logging.getLogger("rag.ingest")
retrieve_logger = logging.getLogger("rag.retrieve")

_EMBED_MODEL = "qllama/bge-small-en-v1.5"

# Ported verbatim from service.go's userPrompt fmt.Sprintf template (byte-for-byte,
# including the Go source's literal tab indentation on every line after the first —
# this string IS the business rule, its exact whitespace affects the prompt hash).
_PROMPT_TEMPLATE = 'Context (retrieved from handbook):\n\t%s\n\t\n\t---\n\t\n\tQuestion: %s\n\t\n\tThink through the following before responding:\n\t1. Is this a casual or conversational question that doesn\'t require handbook knowledge?\n\t2. Does the context contain a clear answer to the question?\n\t3. Is a citation actually necessary to support this answer?\n\t4. Use normal English spacing between words (never merge words, e.g. write "must satisfy" not "mustsatisfy").\n\t\n\tThen respond using this format:\n\t\n\t[Your response here. Only append a citation like (Section X.X, p.N) if the answer references a specific policy, rule, date, or procedure from the handbook. Do not cite for greetings, simple clarifications, or conversational replies.]\n\t'


class RagError(Exception):
    """Raised for RAG-level failures with no more specific module exception:
    currently just the "no search results" case (equivalent of Go's
    fmt.Errorf("no context found; run ingestion first")).

    Carries the rewrite_outcome already computed before the failure, so a
    caller building a Trace (app/main.py's event_stream()) can still record
    what the rewrite step did even though build_prompt raised before
    returning a BuildPromptResult — see docs/evals/phase1-spec.md ticket 3."""

    def __init__(self, message: str, rewrite_outcome: "RewriteOutcome | None" = None) -> None:
        super().__init__(message)
        self.rewrite_outcome = rewrite_outcome


class RewriteError(Exception):
    """Raised when the rewrite LLM call transport-fails, or both parse/validation
    attempts (matching Go's 2-attempt loop) are exhausted."""


class RewriteAction(str, Enum):
    REWRITE_FOR_RETRIEVAL = "rewrite_for_retrieval"
    GRACEFUL_REPLY = "graceful_reply"
    ASK_BETTER_QUESTION = "ask_better_question"


class RewriteResult(BaseModel):
    """action is a plain str (not RewriteAction) so JSON decoding never fails on
    an unrecognized action value, matching Go's json.Unmarshal into a string-typed
    field — validation of the value happens in the switch/if-chain below, not here."""

    action: str = ""
    rewritten_query: str = ""
    assistant_message: str = ""


class RewriteOutcomeStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    RAN = "ran"
    FAILED_FALLBACK = "failed_fallback"


class RewriteOutcome(BaseModel):
    """What build_prompt's rewrite step actually did on this call, so a Trace's
    rewrite Step can distinguish "never configured" from "configured but the
    LLM call failed and fell back to the raw question" — see
    docs/evals/phase1-spec.md story 6."""

    status: RewriteOutcomeStatus
    original_question: str = ""
    rewritten_query: str = ""


class BuildPromptResult(BaseModel):
    """Widened return value for RagService.build_prompt: the assembled prompt
    (or, for a graceful_reply/ask_better_question direct reply, that reply
    text with empty results) plus the rewrite outcome."""

    prompt: str
    results: list[SearchResult]
    rewrite_outcome: RewriteOutcome


@dataclass
class QueryRewriter:
    client: OpenAICompatibleClient
    api_key: str
    model: str


# Ported verbatim from rewrite.go's queryRewritePrompt const (byte-for-byte,
# including box-drawing characters and em dashes) — this is a business-rule
# string fed to the rewrite LLM, not boilerplate.
_QUERY_REWRITE_PROMPT = 'You are a Query Rewriter for a University Handbook RAG assistant.\nTriage every user query into exactly one of three actions.\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nACTIONS\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n1. rewrite_for_retrieval\n   Use when the query has ANY plausible handbook/academic/admin intent.\n   - Strip irrelevant personal context, emotions, or unrelated facts.\n   - Expand abbreviations only when needed for clarity.\n   - Output keyword-style phrases, NOT full sentences or questions.\n   - Add at most 2-3 anchor terms a handbook would use:\n     policy, procedure, requirement, eligibility, deadline,\n     documentation, approval, guideline, criteria\n   - Keep named entities exactly as written (course codes, office names,\n     program names, acronyms, dates).\n   - Do NOT invent facts, rules, offices, or section references.\n   - Length: 4 to 14 words. Hard cap: 18 words.\n   - When in doubt, prefer this action.\n\n2. graceful_reply\n   Use ONLY for greetings or casual social input (hi, thanks, bye).\n   - Respond briefly and redirect to handbook topics.\n\n3. ask_better_question\n   Use ONLY as a last resort for clearly off-topic or non-handbook queries\n   (jokes, entertainment, unrelated facts).\n   - Ask concisely for a handbook-related question.\n   - Do NOT use just because the query is short or vague — rewrite it instead.\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nOUTPUT — valid JSON only, no markdown\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{\n  "action": "rewrite_for_retrieval" | "graceful_reply" | "ask_better_question",\n  "rewritten_query": string,\n  "assistant_message": string\n}\n\nrewrite_for_retrieval → rewritten_query non-empty, assistant_message empty.\ngraceful_reply        → assistant_message non-empty, rewritten_query empty.\nask_better_question   → assistant_message non-empty, rewritten_query empty.\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nEXAMPLES\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nInput:  "when is add drop?"\nOutput: {"action":"rewrite_for_retrieval","rewritten_query":"add/drop deadline course registration change procedure","assistant_message":""}\n\nInput:  "attendance policy?"\nOutput: {"action":"rewrite_for_retrieval","rewritten_query":"attendance policy requirements","assistant_message":""}\n\nInput:  "scholarship"\nOutput: {"action":"rewrite_for_retrieval","rewritten_query":"scholarship eligibility requirements and application deadline","assistant_message":""}\n\nInput:  "I already failed this course once, can I retake it?"\nOutput: {"action":"rewrite_for_retrieval","rewritten_query":"course retake policy GPA impact procedure","assistant_message":""}\n\nInput:  "my advisor said I need 120 credits, how do I apply for graduation?"\nOutput: {"action":"rewrite_for_retrieval","rewritten_query":"graduation application procedure and requirements","assistant_message":""}\n\nInput:  "I\'m so stressed, can I get an incomplete grade?"\nOutput: {"action":"rewrite_for_retrieval","rewritten_query":"incomplete grade request eligibility and approval criteria","assistant_message":""}\n\nInput:  "a friend said tuition is due in August, when exactly?"\nOutput: {"action":"rewrite_for_retrieval","rewritten_query":"tuition payment deadline","assistant_message":""}\n\nInput:  "hi"\nOutput: {"action":"graceful_reply","rewritten_query":"","assistant_message":"Hi! Ask me anything about university policies, deadlines, or procedures."}\n\nInput:  "tell me a joke"\nOutput: {"action":"ask_better_question","rewritten_query":"","assistant_message":"I can only help with university handbook topics. Do you have a question about a policy, deadline, or requirement?"}'

_OFF_TOPIC_SIGNALS = (
    "joke", "poem", "story", "lyrics", "movie", "recipe", "football",
    "cricket", "stock price", "crypto price", "weather", "horoscope",
    "tell me about cats", "tell me about dogs",
)


def _is_clearly_off_topic(q: str) -> bool:
    return any(signal in q for signal in _OFF_TOPIC_SIGNALS)


def _should_fallback_to_rewrite(question: str) -> bool:
    q = question.strip().lower()
    if not q:
        return False
    if _is_clearly_off_topic(q):
        return False
    # For most non-empty user questions, prefer retrieval over redirect.
    return True


def _fallback_rewrite_query(question: str) -> str:
    q = question.strip()
    if not q:
        return "university handbook policy requirement"
    q = q.strip("?!.,;:")
    q = " ".join(q.split())
    if len(q.split()) < 3:
        return q + " university handbook policy"
    return q


async def _rewrite_query_with_llm(
    client: OpenAICompatibleClient, api_key: str, model: str, question: str
) -> RewriteResult:
    parse_err: Exception | None = None
    for attempt in range(1, 3):
        content = await client.complete(
            api_key,
            model,
            0.1,
            [
                ChatMessage(role="system", content=_QUERY_REWRITE_PROMPT),
                ChatMessage(role="user", content=question),
            ],
        )

        try:
            parsed = RewriteResult.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as err:
            parse_err = RewriteError(f"rewrite parse json attempt={attempt}: {err}")
            continue

        parsed.rewritten_query = parsed.rewritten_query.strip()
        parsed.assistant_message = parsed.assistant_message.strip()

        if parsed.action == RewriteAction.REWRITE_FOR_RETRIEVAL:
            if not parsed.rewritten_query:
                parse_err = RewriteError(
                    f"rewrite parse validation attempt={attempt}: empty rewritten_query"
                )
                continue
            return parsed
        elif parsed.action == RewriteAction.GRACEFUL_REPLY:
            if not parsed.assistant_message:
                parse_err = RewriteError(
                    f"rewrite parse validation attempt={attempt}: empty assistant_message"
                )
                continue
            return parsed
        elif parsed.action == RewriteAction.ASK_BETTER_QUESTION:
            if not parsed.assistant_message:
                parse_err = RewriteError(
                    f"rewrite parse validation attempt={attempt}: empty assistant_message"
                )
                continue
            if _should_fallback_to_rewrite(question):
                return RewriteResult(
                    action=RewriteAction.REWRITE_FOR_RETRIEVAL.value,
                    rewritten_query=_fallback_rewrite_query(question),
                    assistant_message="",
                )
            return parsed
        else:
            parse_err = RewriteError(
                f"rewrite parse validation attempt={attempt}: invalid action {parsed.action!r}"
            )

    assert parse_err is not None
    raise parse_err


class RagService:
    """Orchestrator wiring chunk/embed/llm/pdfextract/store together.
    Equivalent of Go's rag.Service."""

    def __init__(
        self,
        store: PostgresStore,
        embed_client: OllamaClient,
        collection: str,
        top_k: int,
        pdf_path: str,
        chunk_words: int = 180,
    ) -> None:
        """chunk_words defaults to 180, matching Go's NewService (which hardcodes
        chunkWords: 180 — see internal/rag/service.go). Every real Go caller
        (cmd/server, cmd/ingest) goes through NewService, so 180 is the true
        production default; chunk.build's own <= 0 -> 350 fallback is dead code
        on that path and only matters if a caller passes chunk_words <= 0
        explicitly (e.g. NewServiceWithParams(0, ...) never appears in Go)."""
        self._store = store
        self._embed_client = embed_client
        self._collection = collection
        self._top_k = top_k
        self._pdf_path = pdf_path
        self._chunk_words = chunk_words
        self._embed_model = _EMBED_MODEL
        self._rewriter: QueryRewriter | None = None

    def set_query_rewriter(
        self, client: OpenAICompatibleClient | None, api_key: str, model: str
    ) -> None:
        api_key = api_key.strip()
        model = model.strip()
        if client is None or not api_key or not model:
            self._rewriter = None
            return
        self._rewriter = QueryRewriter(client=client, api_key=api_key, model=model)

    async def retrieve(self, question: str) -> list[SearchResult]:
        """Embeds the question and returns the raw top-K results with scores."""
        vec = await self._embed_client.embed(self._embed_model, question)
        return await self._store.search(self._collection, vec, self._top_k)

    async def ingest(self) -> int:
        start = time.monotonic()
        ingest_logger.info(
            "start collection=%s pdf=%s embed_model=%s",
            self._collection,
            self._pdf_path,
            self._embed_model,
            extra={"stage": "ingest"},
        )

        await self._store.ensure_schema(self._collection)
        ingest_logger.info("store collection ready: %s", self._collection, extra={"stage": "ingest"})

        pages = await extract_by_page(self._pdf_path)
        ingest_logger.info("extracted pages=%d", len(pages), extra={"stage": "ingest"})

        inputs = [ChunkInput(page=p.page, text=p.text) for p in pages]

        chunks: list[Chunk] = chunk_build(inputs, self._chunk_words)
        ingest_logger.info(
            "built chunks=%d words_per_chunk=%d", len(chunks), self._chunk_words,
            extra={"stage": "ingest"},
        )
        for c in chunks:
            vec = await self._embed_client.embed(self._embed_model, c.text)
            await self._store.upsert(self._collection, c.id, vec, c.text, c.page)
            if c.id > 0 and c.id % 25 == 0:
                ingest_logger.info(
                    "progress upserted_chunks=%d/%d", c.id + 1, len(chunks),
                    extra={"stage": "ingest"},
                )

        duration = time.monotonic() - start
        ingest_logger.info(
            "complete chunks=%d duration=%.3fs", len(chunks), duration,
            extra={"stage": "ingest"},
        )
        return len(chunks)

    async def build_prompt(self, question: str) -> BuildPromptResult:
        start = time.monotonic()
        retrieve_logger.info(
            "start question_chars=%d top_k=%d", len(question), self._top_k,
            extra={"stage": "retrieve"},
        )

        retrieval_query = question
        rewrite_outcome = RewriteOutcome(status=RewriteOutcomeStatus.NOT_CONFIGURED)
        if self._rewriter is not None:
            try:
                rewrite_result = await _rewrite_query_with_llm(
                    self._rewriter.client,
                    self._rewriter.api_key,
                    self._rewriter.model,
                    question,
                )
            except Exception as err:
                retrieve_logger.info(
                    "rewrite failed fallback_original=true error=%s", err,
                    extra={"stage": "retrieve"},
                )
                rewrite_outcome = RewriteOutcome(
                    status=RewriteOutcomeStatus.FAILED_FALLBACK,
                    original_question=question,
                )
            else:
                if rewrite_result.action == RewriteAction.REWRITE_FOR_RETRIEVAL:
                    retrieval_query = rewrite_result.rewritten_query
                    retrieve_logger.info(
                        "triage action=%s original=%r rewritten=%r",
                        rewrite_result.action,
                        question,
                        retrieval_query,
                        extra={"stage": "retrieve"},
                    )
                    rewrite_outcome = RewriteOutcome(
                        status=RewriteOutcomeStatus.RAN,
                        original_question=question,
                        rewritten_query=retrieval_query,
                    )
                else:
                    retrieve_logger.info(
                        "triage action=%s direct_reply=true", rewrite_result.action,
                        extra={"stage": "retrieve"},
                    )
                    rewrite_outcome = RewriteOutcome(
                        status=RewriteOutcomeStatus.RAN,
                        original_question=question,
                    )
                    return BuildPromptResult(
                        prompt=rewrite_result.assistant_message,
                        results=[],
                        rewrite_outcome=rewrite_outcome,
                    )

        query_embedding = await self._embed_client.embed(
            self._embed_model, retrieval_query
        )
        retrieve_logger.info(
            "embedded question vector_dim=%d", len(query_embedding),
            extra={"stage": "retrieve"},
        )

        results = await self._store.search(
            self._collection, query_embedding, self._top_k
        )
        if not results:
            raise RagError(
                "no context found; run ingestion first", rewrite_outcome=rewrite_outcome
            )
        retrieve_logger.info("retrieved context_chunks=%d", len(results), extra={"stage": "retrieve"})

        context = "".join(f"[Page {r.page}]: {r.text}\n\n" for r in results)

        user_prompt = _PROMPT_TEMPLATE % (context, question)

        retrieve_logger.info("assembled prompt_chars=%d", len(user_prompt), extra={"stage": "retrieve"})
        retrieve_logger.info(
            "retrieval complete duration=%.3fs", time.monotonic() - start,
            extra={"stage": "retrieve"},
        )
        return BuildPromptResult(
            prompt=user_prompt, results=results, rewrite_outcome=rewrite_outcome
        )
