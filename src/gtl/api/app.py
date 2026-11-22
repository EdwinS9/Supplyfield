"""FastAPI routes shared by the live control room and local simulations."""

import asyncio
import difflib
import hashlib
import json
import math
import mimetypes
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gtl.api.benefit import benefit, latest_run, learned_rules
from gtl.api.client_orders import delivery_view
from gtl.api.environment import environment_view
from gtl.api.integrations import connected, other_companies, preview as company_preview
from gtl.api.mail import (
    FAST_TRANSLATE_MODELS,
    TRANSLATE_SYSTEM,
    mail_message,
    mail_view,
    parse_translation,
    translate_mail,
)
from gtl.api.procurement import procurement_view
from gtl.api.provenance import provenance_view
from gtl.api.quality import quality_view
from gtl.api.supply import supply_view
from gtl.api.cards import action_summary, card_sources, decision_routing, product_inputs
from gtl.api.source_progress import source_progress
from gtl.api.evidence import excerpt, table_excerpt
from gtl.board import SqliteBoard
from gtl.comms import follow_up
from gtl.comms.agentmail import BASE_URL as AGENTMAIL_BASE_URL
from gtl.comms.explainer import explain
from gtl.govern.policy import load_policy
from gtl.llm import LLMClient, LLMError
from gtl.registry import load_agents
from gtl.contracts import Clock, FileSource, HumanAnswer, LLM, Post
from soony_api import SoonyError
from soony_api.resources.files import safe_local_path


class PreviewInput(BaseModel):
    source: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    locator: str | None = None
    quote: str | None = None
    column: str | None = None
    find: str | None = None


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    post_id: str
    answered_by: str = Field(min_length=1)
    choice: int | None = Field(default=None, ge=0)
    approved: bool | None = None
    text: str = ""
    answered_at: str = ""


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    follow_up: dict
    changed_by: str = Field(default="control_room", min_length=1, max_length=80)


class TranslateMailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = ""
    body: str = ""
    inbox: str | None = None
    message_id: str | None = None
    target_language: str = Field(default="English", min_length=2, max_length=40)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(503, "Artifact is temporarily unreadable") from error


def _archive_path(root: Path, source: str, digest: str | None) -> Path:
    if digest is None:
        current = _read_json(root / "portal-current.json") or {}
        digest = current.get(source)
    if not digest or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise FileNotFoundError(source)
    return safe_local_path(root / "raw" / digest, source)


def _forecast_coverage(forecast: Post | None, products: dict, configured_products: dict) -> dict:
    """Count products with both estimates. A measured zero counts as an estimate."""
    body = forecast.body if forecast else {}
    copies = body["copies"] if isinstance(body.get("copies"), dict) else {}
    shortfall = body["shortfall"] if isinstance(body.get("shortfall"), dict) else {}
    expected = set(configured_products) | set(copies) | set(shortfall)
    expected.update(key for key, fields in products.items() if fields.get("demand_units") is not None)
    covered = sorted(key for key in expected if all(
        type(n) in (int, float) and math.isfinite(n) for n in (copies.get(key), shortfall.get(key))))
    if forecast is None:
        state = "waiting"
    elif not covered:
        state = "incomplete"
    elif len(covered) < len(expected):
        state = "partial"
    else:
        state = "available"
    return {"state": state, "covered_products": covered, "expected_products": len(expected),
            "product_ids": sorted(expected)}


def create_app(board: SqliteBoard, tenant: dict | None = None, *, files: FileSource | None = None, clock: Clock | None = None,
               var_dir: str | Path = "var", playbooks_dir: str | Path = "playbooks",
               tenants_dir: str | Path = "tenants", live_mail: bool = False,
               live_translate: bool | None = None,
               mail_client: httpx.Client | None = None, llm: LLM | None = None) -> FastAPI:
    app = FastAPI(title="Supply chain control room")
    root = Path(var_dir)
    gym = root / "gym"
    zone = ZoneInfo((tenant or {}).get("timezone", "UTC"))
    now = clock.now if clock else lambda: datetime.now(zone)

    # Replay the queue events once, then tail them. An answer stays submitted
    # while another process polls the queue and applies the gate's decision.
    submitted: set[str] = set()
    recent_action_ids: dict[str, None] = {}
    cursor = 0
    last_reader_claim_at = None

    def catch_up() -> None:
        nonlocal cursor, last_reader_claim_at
        for event in board.events_since(cursor):
            cursor = event["id"]
            if event["type"] == "answer.added":
                submitted.add(event["post_id"])
            elif (event["type"] == "post.append" and event["payload"].get("kind") == "claim"
                  and event["actor"] in ("table_reader", "document_reader", "mail_reader")):
                last_reader_claim_at = event["ts"]
            elif event["payload"].get("kind") == "action" and event["type"] in ("post.append", "post.status"):
                action_id = event["post_id"]
                recent_action_ids.pop(action_id, None)
                recent_action_ids[action_id] = None
                if len(recent_action_ids) > 5:
                    del recent_action_ids[next(iter(recent_action_ids))]


    @app.get("/api/state")
    async def state():
        since = board.last_event_id()
        catch_up()
        file_change = board.latest_event_post("file.changed")
        stop = board.latest_event_post("control.pause")
        resume = board.latest_event_post("control.resume")
        # Board post IDs have a globally increasing suffix even when timestamps tie.
        paused = stop is not None and (resume is None or
                 int(stop.id.rsplit("_", 1)[1]) > int(resume.id.rsplit("_", 1)[1]))
        products = {}
        for p in board.query(kind="fact", status="active", subject_prefix="product/", limit=-1):
            b = p.body
            if b.get("entity") == "product" and not b.get("quarantined"):
                products.setdefault(b["key"], {})[b["field"]] = b["value"]
        forecasts = board.query(kind="forecast", status="current")
        forecast = forecasts[-1] if forecasts else None
        parts = {}
        limiting_parts = forecast.body.get("limiting_part", {}) if forecast else {}
        for part in set(limiting_parts.values()):
            if not part:
                continue
            for fact in board.query(kind="fact", status="active", subject_prefix=f"part/{part}/description"):
                if fact.body.get("field") == "description" and not fact.body.get("quarantined"):
                    parts[part] = fact.body.get("value")
        risks = board.query(kind="defect", status="open", limit=-1)
        risks += board.query(kind="defect", status="requested", limit=-1)
        risks.sort(key=lambda p: p.body.get("copies_at_risk", 0), reverse=True)
        deadlines = []
        for product in products.values():
            try:
                stamp = datetime.fromisoformat(product["deadline"])
                if stamp.tzinfo:
                    deadlines.append(stamp)
            except (KeyError, TypeError, ValueError):
                continue
        recent_actions = []
        for action_id in reversed(recent_action_ids):
            action = board.get(action_id)
            if action:
                recent_actions.append(action_summary(board, action))
        return {"now": now().isoformat(), "deadline": min(deadlines).isoformat() if deadlines else None,
                "tenant_name": (tenant or {}).get("name") or (tenant or {}).get("id"),
                "forecast_coverage": _forecast_coverage(forecast, products, (tenant or {}).get("value_per_copy", {})),
                "forecast_at": forecast.created_at if forecast else None,
                "last_file_change_at": file_change.created_at if file_change else None,
                "last_reader_claim_at": last_reader_claim_at,
                "products": products, "parts": parts, "timezone": zone.key,
                "value_per_copy": (tenant or {}).get("value_per_copy", {}),
                "forecast": forecast.body if forecast else None, "forecast_id": forecast.id if forecast else None,
                "risks": [p.to_dict() for p in risks[:5]], "recent_actions": recent_actions,
                "paused": paused, "event_id": since}

    @app.get("/api/posts")
    def posts(kind: str | None = None, status: str | None = None, since_event: int = Query(0, ge=0),
              limit: int = Query(1000, ge=1, le=10000)):
        return board.query(kind=kind, status=status, since_event=since_event, limit=limit)

    @app.get("/api/feed")
    def feed(limit: int = Query(50, ge=1, le=200)):
        # Most recently changed posts, including updates to older cards.
        end = board.last_event_id()
        start = max(0, end - limit * 3)
        events = board.events_since(start)
        ids = dict.fromkeys(e["post_id"] for e in reversed(events) if e.get("post_id"))
        while start and len(ids) < limit:
            end, start = start, max(0, start - max(limit * 3, len(events)))
            events = board.events_since(start)
            ids.update((e["post_id"], None) for e in reversed(events) if e["id"] <= end and e.get("post_id"))
        posts = [post for key in list(ids)[:limit] if (post := board.get(key)) is not None]
        # The page offers a Source link only where following the card's records finds something to show.
        return [{**post.to_dict(), "has_sources": any(card_sources(board, [post.id]).values())} for post in posts]

    @app.get("/api/posts/{post_id}")
    def post(post_id: str):
        item = board.get(post_id)
        if item is None:
            raise HTTPException(404, "Unknown post")
        return item

    @app.get("/api/events/stream")
    async def stream(request: Request, since_event: int = Query(0, ge=0),
                     last_event_id: int | None = Header(default=None, ge=0)):
        async def events():
            cursor = since_event if last_event_id is None else last_event_id
            idle = 0
            while not await request.is_disconnected():
                for event in board.events_since(cursor):
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
                    idle = 0
                if idle % 15 == 0:
                    yield ": connected\n\n"
                idle += 1
                await asyncio.sleep(1)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/inbox")
    async def inbox(role: str | None = None):
        catch_up()
        items = board.query(kind="decision", status="open", limit=-1)
        items += board.query(kind="defect", status="requested", limit=-1)
        items = [p for p in items if p.id not in submitted and
                 (not role or role == (p.body.get("approver_role") or p.body.get("owner_role")))]
        result = [{**p.to_dict(), "routing": decision_routing(board, p)} if p.kind == "decision" else p.to_dict()
                  for p in items]
        for notice in board.query(kind="notice", status="posted", limit=-1):
            question = _open_question(notice)
            if question is not None and question.id in submitted:
                continue
            if question is not None and role and role != question.body.get("to_id"):
                continue
            item = notice.to_dict()
            if question is not None:
                item["question"] = {"id": question.id, "role": question.body.get("to_id"),
                                    "copies_at_risk": question.body.get("copies_at_risk"),
                                    "deadline": question.body.get("deadline")}
            result.append(item)
        return result

    def _open_question(notice: Post) -> Post | None:
        """The question a notice relays, when it is still waiting for a role in the control room."""
        for ref in notice.depends_on:
            question = board.get(ref)
            if (question is not None and question.kind == "question" and question.status == "asked"
                    and question.body.get("to_kind") == "role" and question.body.get("to_id")):
                return question
        return None

    @app.post("/api/answers", status_code=202)
    async def answer(data: AnswerInput):
        catch_up()
        p = board.get(data.post_id)
        if p is None:
            raise HTTPException(404, "Unknown post")
        open_states = {("decision", "open"), ("defect", "requested"), ("question", "asked")}
        if p.id in submitted or (p.kind, p.status) not in open_states:
            raise HTTPException(409, "This item is no longer awaiting an answer")
        if p.kind == "question":
            if p.body.get("to_kind") != "role":
                raise HTTPException(409, "This question is answered by mail")
            role = p.body.get("to_id")
        else:
            role = p.body.get("approver_role") if p.kind == "decision" else p.body.get("owner_role")
        if role != data.answered_by:
            raise HTTPException(403, "This item is assigned to another role")
        if p.kind == "question" and (data.choice is not None or not data.text.strip()):
            raise HTTPException(422, "Write an answer to the question")
        options = p.body.get("options") if p.kind == "decision" else None
        if options:
            if data.choice is None or data.choice >= len(options):
                raise HTTPException(422, "Choose an available option")
        elif data.choice is not None or (data.approved is None and not data.text.strip()):
            raise HTTPException(422, "Supply an approval or a written answer")
        values = data.model_dump()
        values["answered_at"] = now().isoformat()
        board.add_answer(HumanAnswer(**values))
        return {"post_id": p.id}

    policies: list = []

    def policy():
        """The tenant's approval rules, read once; None when the tenant has none on file."""
        if not policies:
            try:
                policies.append(load_policy(str((tenant or {}).get("id"))))
            except (OSError, ValueError, KeyError):
                policies.append(None)
        return policies[0]

    def explained(action):
        return explain(board, tenant or {}, policy(), action, now())

    @app.get("/api/procurement")
    def procurement():
        """Products before and after, every inbound order with and without the agents, and their steps."""
        return {**procurement_view(board, tenant, explained), "now": now().isoformat()}

    @app.get("/api/delivery")
    def delivery():
        """What the clients ordered and when they can expect it, without and with the agents."""
        return {**delivery_view(board, tenant, explained), "now": now().isoformat(),
                "tenant_name": (tenant or {}).get("name") or (tenant or {}).get("id")}

    @app.get("/api/supply")
    def supply():
        """Stock per part, purchases with their prices, and price negotiations with suppliers."""
        return supply_view(board)

    provider_client: list[httpx.Client | None] = [mail_client]
    mail_cache: dict[str, object] = {"at": 0.0, "event_id": -1, "data": None}

    def _mail_http() -> httpx.Client | None:
        if provider_client[0] is not None:
            return provider_client[0]
        if not live_mail:
            return None
        key = os.environ.get("AGENTMAIL_API_KEY", "").strip()
        if not key:
            return None
        provider_client[0] = httpx.Client(
            base_url=AGENTMAIL_BASE_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=15.0,
        )
        return provider_client[0]

    @app.get("/api/mail")
    def mail_traffic(refresh: bool = False):
        """Structured email traffic sent by and received by the agents."""
        http = _mail_http()
        event_id = board.last_event_id()
        now_mono = time.monotonic()
        cached = mail_cache["data"]
        if (not refresh and cached is not None and mail_cache["event_id"] == event_id
                and (http is None or now_mono - float(mail_cache["at"]) < 15.0)):
            return cached
        view = mail_view(board, tenant, var_dir=root, client=http)
        mail_cache.update(at=now_mono, event_id=event_id, data=view)
        return view

    @app.get("/api/mail/message")
    def mail_detail(inbox: str = Query(..., min_length=1), message_id: str = Query(..., min_length=1)):
        """Full body and metadata for one email."""
        detail = mail_message(board, tenant, inbox=inbox, message_id=message_id, client=_mail_http())
        if detail is None:
            raise HTTPException(404, "Email message not found")
        return detail

    translate_enabled = live_mail if live_translate is None else live_translate
    llm_holder: list[LLM | None] = [llm]
    fast_openai: dict[str, object] = {"ready": False, "client": None, "models": []}
    translation_cache_path = root / "mail-translations.json"
    translation_cache: dict[str, dict] = {}
    if translation_cache_path.is_file():
        try:
            loaded = json.loads(translation_cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                translation_cache.update(loaded)
        except (OSError, ValueError):
            pass

    def _save_translation_cache() -> None:
        if not translate_enabled:
            return
        try:
            translation_cache_path.parent.mkdir(parents=True, exist_ok=True)
            translation_cache_path.write_text(
                json.dumps(translation_cache, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass

    def _fast_translator() -> tuple[object | None, list[str]]:
        """Direct OpenAI client ordered with the fastest chat models on the configured endpoint."""
        if fast_openai["ready"]:
            return fast_openai["client"], list(fast_openai["models"])  # type: ignore[arg-type]
        fast_openai["ready"] = True
        if llm_holder[0] is not None or not translate_enabled:
            return None, []
        base_url = (os.environ.get("LLM_BASE_URL") or "").strip()
        api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("RADEON_API_KEY") or "").strip()
        if not base_url or not api_key:
            return None, []
        from openai import OpenAI
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=8.0, max_retries=0)
        candidates: list[str] = []
        override = (os.environ.get("LLM_TRANSLATE_MODEL") or "").strip()
        if override:
            candidates.append(override)
        try:
            resp = httpx.get(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=5.0)
            available = {str(m.get("id")) for m in resp.json().get("data", []) if isinstance(m, dict)} if resp.is_success else set()
        except (httpx.HTTPError, ValueError):
            available = set()
        for name in FAST_TRANSLATE_MODELS:
            if name not in candidates and (not available or name in available):
                candidates.append(name)
        for env_name in ("LLM_ALT_MODEL", "LLM_MODEL"):
            val = (os.environ.get(env_name) or "").strip()
            if val and val not in candidates:
                candidates.append(val)
        fast_openai["client"] = client
        fast_openai["models"] = candidates
        return client, candidates

    def _translator() -> LLM | None:
        if llm_holder[0] is not None:
            return llm_holder[0]
        if not translate_enabled:
            return None
        llm_holder[0] = LLMClient(on_call=lambda payload: board.log_event("mail_translator", "llm.call", payload))
        return llm_holder[0]

    def _resolve_translate_input(data: TranslateMailInput) -> tuple[str, str, str, str]:
        subject, body = data.subject, data.body
        if not body.strip() and data.inbox and data.message_id:
            detail = mail_message(board, tenant, inbox=data.inbox, message_id=data.message_id, client=_mail_http())
            if detail is not None:
                subject = subject or detail.get("subject", "")
                body = detail.get("body", "")
        if not subject.strip() and not body.strip():
            raise HTTPException(422, "Nothing to translate")
        target = data.target_language.strip()
        digest = hashlib.sha256(f"{subject}\0{body}".encode("utf-8")).hexdigest()
        cache_key = f"{target.casefold()}:{digest}"
        return subject, body, target, cache_key

    def _fast_translate_sync(subject: str, body: str, target: str) -> dict | None:
        client, models = _fast_translator()
        if client is None or not models:
            return None
        messages = [
            {"role": "system", "content": TRANSLATE_SYSTEM.format(target_language=target)},
            {"role": "user", "content": f"Subject: {subject}\n\n{body}".strip()},
        ]
        for model in models:
            try:
                resp = client.chat.completions.create(  # type: ignore[attr-defined]
                    model=model, messages=messages, temperature=0.0, max_tokens=1000,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                raw = (resp.choices[0].message.content or "") if resp.choices else ""
                return parse_translation(raw, subject, target, model=model)
            except Exception:  # noqa: BLE001 - fall through to next fast model
                continue
        return None

    @app.post("/api/mail/translate")
    def mail_translate(data: TranslateMailInput):
        """Translate an email on the spot into the target language."""
        subject, body, target, cache_key = _resolve_translate_input(data)
        if cache_key in translation_cache:
            return translation_cache[cache_key]
        fast = _fast_translate_sync(subject, body, target)
        if fast is not None:
            translation_cache[cache_key] = fast
            _save_translation_cache()
            return fast
        client = _translator()
        if client is None:
            raise HTTPException(503, "Translation model is not configured")
        try:
            translated = translate_mail(subject, body, client, target_language=target)
        except LLMError as exc:
            raise HTTPException(503, f"Translation unavailable: {exc}") from None
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from None
        translation_cache[cache_key] = translated
        _save_translation_cache()
        return translated

    @app.post("/api/mail/translate/stream")
    def mail_translate_stream(data: TranslateMailInput):
        """Stream an on-the-spot email translation as NDJSON events for instant UI feedback."""
        subject, body, target, cache_key = _resolve_translate_input(data)

        def generate():
            if cache_key in translation_cache:
                yield json.dumps({"type": "done", **translation_cache[cache_key]}) + "\n"
                return
            client, models = _fast_translator()
            if client is not None and models:
                messages = [
                    {"role": "system", "content": TRANSLATE_SYSTEM.format(target_language=target)},
                    {"role": "user", "content": f"Subject: {subject}\n\n{body}".strip()},
                ]
                for model in models:
                    chunks: list[str] = []
                    try:
                        stream = client.chat.completions.create(  # type: ignore[attr-defined]
                            model=model, messages=messages, temperature=0.0, max_tokens=1000,
                            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                            stream=True,
                        )
                        for part in stream:
                            delta = part.choices[0].delta.content if part.choices else None
                            if delta:
                                chunks.append(delta)
                                yield json.dumps({"type": "chunk", "text": delta}) + "\n"
                        parsed = parse_translation("".join(chunks), subject, target, model=model)
                        translation_cache[cache_key] = parsed
                        _save_translation_cache()
                        yield json.dumps({"type": "done", **parsed}) + "\n"
                        return
                    except Exception:  # noqa: BLE001 - only fall through if nothing was streamed yet
                        if chunks:
                            break
                        continue
            fallback_llm = _translator()
            if fallback_llm is None:
                yield json.dumps({"type": "error", "detail": "Translation model is not configured"}) + "\n"
                return
            try:
                translated = translate_mail(subject, body, fallback_llm, target_language=target)
            except (LLMError, ValueError) as exc:
                yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"
                return
            translation_cache[cache_key] = translated
            _save_translation_cache()
            yield json.dumps({"type": "done", **translated}) + "\n"

        return StreamingResponse(generate(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/integrations")
    def integrations():
        return {**connected(tenant), "others": other_companies(Path(tenants_dir), (tenant or {}).get("id"))}

    @app.get("/api/integrations/{tenant_id}/preview")
    def integration_preview(tenant_id: str):
        view = company_preview(Path(tenants_dir), tenant_id, cache_dir=root)
        if view is None:
            raise HTTPException(404, "Unknown company")
        return view

    @app.get("/api/environment")
    def environment():
        return environment_view(board, load_agents(), gym)

    @app.get("/api/provenance")
    def provenance():
        """Whether the screens show the customer's real files or a fictional sample, and which run."""
        return provenance_view(board, tenant, root)

    @app.get("/api/quality")
    def data_quality():
        return quality_view((tenant or {}).get("sources", {}).get("data_root"), as_of=now().date())

    @app.get("/api/source-progress")
    def file_progress():
        return {"files": source_progress(board), "checked_at": now().isoformat()}

    @app.get("/api/card-sources")
    def sources(post_id: str | None = None, product: str | None = None):
        post = board.get(post_id) if post_id else None
        if post_id and post is None:
            raise HTTPException(404, "Unknown post")
        if product and (post is None or post.kind == "forecast"):
            return card_sources(board, product_inputs(board, product, post))
        if post is None:
            raise HTTPException(422, "Choose a card or product")
        return card_sources(board, [post.id])

    def evidence_path(source: str, digest: str | None) -> Path:
        """The archived version of a file. Without an archive, the current file in the tenant's data folder."""
        try:
            path = _archive_path(root, source, digest)
            if path.is_file():
                return path
        except FileNotFoundError:
            pass
        data_root = (tenant or {}).get("sources", {}).get("data_root")
        if digest is None and data_root:
            path = safe_local_path(Path(data_root), source)
            if path.is_file():
                return path
        raise FileNotFoundError(source)

    @app.get("/api/evidence")
    def evidence(source: str, sha256: str | None = Query(None, pattern=r"^[0-9a-f]{64}$")):
        media_type = mimetypes.guess_type(source)[0] or "application/octet-stream"
        # Only passive formats can open in this origin. Download active content.
        inline = media_type in {"application/pdf", "application/json", "text/plain", "text/csv",
                                "image/png", "image/jpeg", "image/gif", "image/webp"}
        disposition = "inline" if inline else "attachment"
        headers = {"X-Content-Type-Options": "nosniff", "Content-Disposition": disposition}
        try:
            if files is not None:
                return Response(files.read(source, sha256), media_type=media_type, headers=headers)
            path = evidence_path(source, sha256)
        except (FileNotFoundError, KeyError, SoonyError, ValueError):
            raise HTTPException(404, "Evidence file unavailable") from None
        return FileResponse(path, media_type=media_type, filename=Path(source).name,
                            content_disposition_type=disposition, headers={"X-Content-Type-Options": "nosniff"})

    @app.post("/api/evidence/preview")
    def preview(ref: PreviewInput):
        try:
            data = files.read(ref.source, ref.sha256) if files is not None else (
                evidence_path(ref.source, ref.sha256).read_bytes())
        except (FileNotFoundError, IsADirectoryError, KeyError, SoonyError, ValueError):
            raise HTTPException(404, "Evidence file unavailable") from None
        try:
            found = excerpt(ref.source, data, ref.locator or "", ref.quote or "")
            if ref.column is None and ref.find is None:
                return found
            return {**found, "table": table_excerpt(ref.source, data, ref.locator or "", ref.column or "", ref.find or "")}
        except Exception:
            # PDF, spreadsheet and document parsers each raise different errors for damaged files.
            raise HTTPException(422, "Source preview unavailable") from None

    def config_view() -> dict:
        events = board.events_since(0)
        rules = follow_up.settings(tenant or {}, events)
        view = {"follow_up": rules.to_dict(), "changed_by": None, "changed_at": None,
                "limits": {"max_asks": follow_up.MAX_ASKS, "max_wait_hours": follow_up.MAX_WAIT_HOURS}}
        for event in reversed(events):
            changed = event["payload"].get("follow_up") if event["type"] == follow_up.CONFIG_EVENT else None
            if changed is not None and follow_up.parse(changed) is not None:
                view.update(changed_by=event["actor"], changed_at=event["ts"])
                break
        return view

    @app.get("/api/config")
    def read_config():
        return config_view()

    @app.put("/api/config")
    def write_config(data: ConfigInput):
        rules = follow_up.parse(data.follow_up, follow_up.settings(tenant or {}, board.events_since(0)))
        if rules is None or set(data.follow_up) - {"asks", "wait_hours"}:
            raise HTTPException(422, "Use 1 to %d emails and a wait above 0 and up to %d hours after each"
                                % (follow_up.MAX_ASKS, follow_up.MAX_WAIT_HOURS))
        board.log_event(data.changed_by, follow_up.CONFIG_EVENT, {"follow_up": rules.to_dict()})
        return config_view()

    @app.post("/api/control/pause")
    async def pause():
        board.append(Post("event", "new", "control.pause", {}, "control_room"))
        return {"paused": True}

    @app.post("/api/control/resume")
    async def resume():
        board.append(Post("event", "new", "control.resume", {}, "control_room"))
        return {"paused": False}

    @app.get("/api/gym/runs")
    def runs():
        if not gym.is_dir():
            return []
        checkpoints = []
        for directory in gym.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            # Include a trainer initializing its first generation, and older curve-only runs.
            stamps = []
            for name in ("curve.json", "state.json"):
                artifact = directory / name
                try:
                    if artifact.is_file() and not artifact.is_symlink():
                        stamps.append(artifact.stat().st_mtime_ns)
                except FileNotFoundError:
                    continue
            if stamps:
                checkpoints.append((max(stamps), directory.name))
        return [name for _, name in sorted(checkpoints, reverse=True)]

    @app.get("/api/gym/runs/{run_id}")
    def run(run_id: str):
        if run_id not in runs():
            raise HTTPException(404, "Unknown Gym run")
        directory = gym / run_id
        return {"id": run_id, "curve": _read_json(directory / "curve.json"), "playbook_diffs": playbook_diffs(directory)}

    def playbook_diffs(directory: Path) -> dict[str, list[str]]:
        diffs = {}
        for best in sorted((directory / "best").glob("*.md")):
            original = Path(playbooks_dir) / best.name
            before = original.read_text().splitlines() if original.is_file() else []
            diffs[best.stem] = list(difflib.unified_diff(before, best.read_text().splitlines(),
                                                        fromfile="original", tofile="learned", lineterm=""))
        return diffs

    @app.get("/api/benefit")
    def agent_benefit(run: str | None = None):
        """No agents, fixed rules, untrained and trained agents, scored on the same validation scenarios."""
        run_id = run or latest_run(gym)
        if run_id is None:
            return {"available": False}
        if run_id not in runs():
            raise HTTPException(404, "Unknown Gym run")
        curve = _read_json(gym / run_id / "curve.json")
        summary = benefit(curve) if isinstance(curve, dict) else None
        if summary is None:
            return {"available": False, "run_id": run_id}
        return {"available": True, **summary, "run_id": run_id,
                "learned": learned_rules(playbook_diffs(gym / run_id))}

    repo = Path(__file__).resolve().parents[3]
    web = repo / "web"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="control-room")
    return app
