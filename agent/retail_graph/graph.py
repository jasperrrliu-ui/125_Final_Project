"""Policy-driven LangGraph for the ABCD retail support demo.

Nodes: understand -> route -> (query -> route)* -> (authorize -> route)* -> reply

* ``understand`` asks the interpreter (LLM or scripted double) what the
  customer said: identity details, new requests, details for the open
  request, answers to questions, an option choice, a yes/no confirmation.
* ``route`` asks the same ladder of questions for every intent:
    1. Is there a request to work on?
    2. Is the customer verified (policy identity methods)?
    3. Are the request's targets known (order, item, coupon)?
    4. Does the policy engine have every fact it needs?
    5. What does the policy decide: decline, handoff, or options?
    6. Has the customer chosen an option and said yes?
  Intent-specific knowledge lives only in the policy file.
* ``query`` runs one read-only backend tool.
* ``authorize`` sends the confirmed option to the policy gate, which
  re-reads records, re-runs the policy, and writes the ledger.
* ``reply`` joins the customer-facing message for this turn.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import re
from typing import Annotated, Any, Callable, Protocol, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent.action_log.policy_gate import AuthorizationRequest, PolicyGate
from agent.policy.engine import evaluate, resolve_facts
from agent.policy.records import coupon_record, customer_record, order_address, order_record
from agent.retail_tools.check_coupon import check_coupon
from agent.retail_tools.read_tools import lookup_order, verify_customer


OUT_OF_SCOPE = "out_of_scope"
IDENTITY_FIELDS = ("email", "full_name", "zip_code", "account_id")
ACTIVE_STATUSES = {"open", "waiting", "offered"}
TARGET_FIELDS = ("order_id", "item_name", "coupon_code")
TOOL_FOR_SOURCE = {"customer": "verify_customer", "order": "lookup_order",
                   "coupon_check": "check_coupon"}

GREETING = ("How can I help you today? I can help with returns, promo codes that "
            "didn't work, cancelling an item, or changing a delivery date.")
HANDOFF_MESSAGE = ("I can't resolve this automatically, so I'm transferring you to a "
                   "human agent who can help.")
SIMULATION_NOTE = "(Simulation: no real order, payment, or coupon was changed.)"
_STOPWORDS = {"the", "a", "an", "my", "this", "that", "item", "one", "please"}


class ChatState(TypedDict, total=False):
    messages: Annotated[list[Any], add_messages]
    identity_claims: dict[str, str]
    identity_status: str | None
    customer_id: str | None
    member_level: str | None
    requests: list[dict[str, Any]]
    next_step: str
    next_tool: str | None
    response_parts: list[str]
    reply_text: str
    cp1_events: list[dict[str, Any]]
    cp2_events: list[dict[str, Any]]


class Interpreter(Protocol):
    def extract(self, latest_message: str, state: ChatState) -> dict[str, Any]:
        """Return what the customer said this turn.

        Keys (all optional): identity {email, full_name, zip_code, account_id};
        requests [{intent, order_id, item_name, coupon_code}]; fields
        {order_id, item_name, coupon_code} for the open request;
        customer_facts {fact_name: value}; selected_option (an offered
        option_id); confirmation ("yes" | "no"); withdraw_active_request
        (bool); out_of_scope (short description of an unsupported request).
        """


def _active_index(requests: list[dict[str, Any]]) -> int | None:
    return next((i for i, r in enumerate(requests) if r["status"] in ACTIVE_STATUSES), None)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold().replace("_", " ")))


def match_item(name: str | None, items: list[dict[str, Any]]) -> int | None:
    """Match a spoken item name to exactly one order item, or return None."""
    if not name:
        return None
    wanted = _tokens(name) - _STOPWORDS
    if not wanted:
        return None
    matches = [x for x in items if wanted <= _tokens(x["name"])]
    return matches[0]["item_index"] if len(matches) == 1 else None


def _fingerprint(options: list[dict[str, Any]] | None) -> list[tuple]:
    return [(o["option_id"], o["action"], sorted(o["payload"].items())) for o in options or []]


def _offer_text(options: list[dict[str, Any]]) -> str:
    if len(options) == 1:
        return f"Here's what I can do: {options[0]['summary']} Shall I go ahead? (yes/no)"
    lines = [f"{i}. {o['summary']}" for i, o in enumerate(options, 1)]
    return ("Here are your options:\n" + "\n".join(lines)
            + "\nWhich one would you like, and shall I go ahead?")


def build_graph(
    interpreter: Interpreter,
    policy_gate: PolicyGate,
    checkpointer: Any,
    *,
    customer_lookup: Callable[[dict[str, str]], dict] = verify_customer,
    order_lookup: Callable[..., dict] = lookup_order,
    coupon_lookup: Callable[..., dict] = check_coupon,
):
    """Compile a graph. The caller owns the checkpointer's lifecycle."""
    policy = policy_gate.policy
    customer_fact_names = {
        name for name, spec in policy.facts.items() if "customer" in spec["sources"]
    }

    # ------------------------------------------------------------ understand
    def understand(state: ChatState) -> dict:
        latest = next((m.content for m in reversed(state["messages"])
                       if isinstance(m, HumanMessage)), "")
        parsed = interpreter.extract(str(latest), state) or {}
        requests = deepcopy(state.get("requests", []))
        parts: list[str] = []
        update: dict[str, Any] = {}

        claims = dict(state.get("identity_claims") or {})
        new_claims = {k: v.strip() for k, v in (parsed.get("identity") or {}).items()
                      if k in IDENTITY_FIELDS and isinstance(v, str) and v.strip()}
        if state.get("customer_id"):
            # G2: one verified customer per conversation.
            conflicting = [k for k in ("email", "full_name")
                           if k in new_claims and claims.get(k)
                           and new_claims[k].casefold() != claims[k].casefold()]
            if conflicting:
                parts.append("I can only help with the account already verified in this "
                             "conversation.")
        elif new_claims:
            if any(claims.get(k) != v for k, v in new_claims.items()):
                update["identity_status"] = None  # verify again with new details
            claims.update(new_claims)
        update["identity_claims"] = claims

        active_before = _active_index(requests)
        for item in parsed.get("requests") or []:
            intent = item.get("intent")
            if intent not in policy.intents:
                continue
            duplicate = any(
                r["status"] in ACTIVE_STATUSES and r["intent"] == intent
                and r.get("order_id") == item.get("order_id")
                and r.get("coupon_code") == item.get("coupon_code")
                for r in requests
            )
            if duplicate:
                continue
            requests.append({
                "id": f"r{len(requests) + 1}",
                "intent": intent,
                "order_id": item.get("order_id"),
                "item_name": item.get("item_name"),
                "item_index": None,
                "coupon_code": item.get("coupon_code"),
                "customer_facts": {},
                "status": "open",
            })
        if isinstance(parsed.get("out_of_scope"), str) and parsed["out_of_scope"].strip():
            requests.append({"id": f"r{len(requests) + 1}", "intent": OUT_OF_SCOPE,
                             "description": parsed["out_of_scope"].strip(),
                             "status": "open"})

        # Details in this message belong to the request that was already open;
        # if none was open, to the first request raised in this message.
        index = active_before if active_before is not None else next(
            (i for i, r in enumerate(requests)
             if r["status"] in ACTIVE_STATUSES and r["intent"] != OUT_OF_SCOPE), None)
        if index is not None:
            request = requests[index]
            changed = False
            fields = parsed.get("fields") or {}
            for key in TARGET_FIELDS:
                value = fields.get(key)
                if isinstance(value, str) and value.strip() and value != request.get(key):
                    request[key] = value.strip()
                    changed = True
                    if key == "order_id":
                        request.pop("order_result", None)
                        request["item_index"] = None
                    elif key == "coupon_code":
                        request.pop("coupon_result", None)
                    elif key == "item_name":
                        request["item_index"] = None
            known = request.get("customer_facts") or {}
            stated = {k: v for k, v in (parsed.get("customer_facts") or {}).items()
                      if k in customer_fact_names and v is not None and known.get(k) != v}
            if stated:
                request.setdefault("customer_facts", {}).update(stated)
                changed = True
            if parsed.get("withdraw_active_request"):
                request["withdraw"] = True
            elif changed:
                # New details can change the decision; offer again.
                request["status"] = "open"
                for key in ("offered_options", "selected_option_id", "confirmation"):
                    request.pop(key, None)
            elif request["status"] == "offered":
                choice = parsed.get("selected_option")
                if isinstance(choice, str) and choice.strip():
                    request["selected_option_id"] = choice.strip()
                if parsed.get("confirmation") in {"yes", "no"}:
                    request["confirmation"] = parsed["confirmation"]
            elif request["status"] == "waiting":
                request["status"] = "open"

        update.update({"requests": requests, "response_parts": parts})
        return update

    # ----------------------------------------------------------------- route
    def route(state: ChatState, config: RunnableConfig) -> dict:
        thread_id = config["configurable"]["thread_id"]
        requests = deepcopy(state.get("requests", []))
        parts = list(state.get("response_parts") or [])
        cp1 = list(state.get("cp1_events") or [])
        cp2 = list(state.get("cp2_events") or [])
        customer_id = state.get("customer_id")
        claims = state.get("identity_claims") or {}
        step, tool = "reply", None

        def log(request: dict | None, next_step: str, *, planned_tool: str | None = None,
                asked: str | None = None) -> None:
            cp1.append({"request_id": request["id"] if request else None,
                        "intent": request["intent"] if request else None,
                        "next_step": next_step, "planned_tool": planned_tool,
                        "asked": asked})

        def ask(request: dict, text: str, asked: str) -> None:
            request["status"] = "waiting" if request["status"] != "offered" else "offered"
            parts.append(text)
            log(request, "reply", asked=asked)

        def close(request: dict, outcome: str, reason: str, message: str,
                  rule_id: str | None = None) -> None:
            request["status"] = outcome
            parts.append(message)
            policy_gate.record_outcome(
                outcome=outcome, conversation_id=thread_id,
                request_id=f"{thread_id}:{request['id']}:final", intent=request["intent"],
                reason_code=reason, customer_id=customer_id,
                order_id=request.get("order_id"), policy_rule_id=rule_id,
            )
            cp2.append({"request_id": request["id"], "intent": request["intent"],
                        "decision": outcome, "rule_id": rule_id, "reason_code": reason})

        # Tell the customer right away about requests we cannot handle, rather
        # than after the current request is finished.
        for request in requests:
            if request["intent"] == OUT_OF_SCOPE and request["status"] in ACTIVE_STATUSES:
                close(request, "handoff", "out_of_scope",
                      f"I can't help with \"{request['description']}\" here; a human "
                      "agent will follow up on that.")

        while True:
            index = _active_index(requests)
            if index is None:
                if not parts:
                    parts.append(GREETING)
                log(None, "reply")
                break
            request = requests[index]

            # 0. Customer withdrew, or the request is outside the policy.
            if request.pop("withdraw", False):
                close(request, "customer_declined", "customer_withdrew",
                      "Okay, I've dropped that request.")
                continue

            # 2. Identity.
            if not customer_id:
                status = state.get("identity_status")
                method = policy.identity_method(claims)
                if method and status is None:
                    step, tool = "query", "verify_customer"
                    log(request, step, planned_tool=tool)
                    break
                if status in {"not_found", "multiple_candidates"}:
                    ask(request, "I couldn't match those details to a single account. "
                        + policy.identity_question, "identity")
                else:
                    ask(request, policy.identity_question, "identity")
                break

            # 3. Targets.
            targets = policy.targets(request["intent"])
            order_facts = None
            context: dict[str, Any] = {}
            if "order_id" in targets:
                if not request.get("order_id"):
                    ask(request, "Which order is this about? Please give me the order ID.",
                        "order_id")
                    break
                if "order_result" not in request:
                    step, tool = "query", "lookup_order"
                    log(request, step, planned_tool=tool)
                    break
                if request["order_result"]["status"] != "found":
                    ask(request, f"I couldn't find order {request['order_id']} on your "
                        "account. Please check the order ID.", "order_id")
                    break
                order_facts = request["order_result"]["facts"]
                context["address"] = order_address(order_facts)
            if "item" in targets:
                items = order_facts["items"]
                if request.get("item_index") is None:
                    request["item_index"] = match_item(request.get("item_name"), items)
                if request.get("item_index") is None and len(items) == 1:
                    request["item_index"] = items[0]["item_index"]
                item = next((x for x in items if x["item_index"] == request.get("item_index")), None)
                if item is None:
                    names = ", ".join(x["name"].replace("_", " ") for x in items)
                    ask(request, f"Which item is this about? The order has: {names}.", "item")
                    break
                context["item"] = item
            if "coupon_code" in targets and not request.get("coupon_code"):
                ask(request, "Which promo code didn't work? Please type the code.", "coupon_code")
                break

            # 4-5. Facts and policy decision.
            records: dict[str, Any] = {"customer": customer_record(state.get("member_level"))}
            if order_facts is not None:
                records["order"] = order_record(order_facts)
            if "coupon_code" in targets and "coupon_result" in request:
                records["coupon_check"] = coupon_record(request["coupon_result"])
            facts = resolve_facts(policy, records, request.get("customer_facts") or {})
            decision = evaluate(policy, request["intent"], facts, context)

            if decision.kind == "need":
                system_needs = [n for n in decision.need if n.source == "system"]
                if system_needs:
                    step, tool = "query", TOOL_FOR_SOURCE[system_needs[0].tool]
                    log(request, step, planned_tool=tool)
                    break
                questions = [n.question for n in decision.need if n.question]
                request["asked_facts"] = [n.name for n in decision.need]
                ask(request, " ".join(questions), "facts:" + ",".join(request["asked_facts"]))
                break
            if decision.kind == "decline":
                close(request, "declined", decision.reason_code or "policy_decline",
                      decision.message or "I'm sorry, I can't do that under our policy.",
                      decision.rule_id)
                continue
            if decision.kind == "handoff":
                close(request, "handoff", decision.reason_code or "policy_handoff",
                      HANDOFF_MESSAGE, decision.rule_id)
                continue

            # 6. Options: offer, then wait for a choice and an explicit yes.
            offered = [asdict(o) for o in decision.options]
            if (request["status"] != "offered"
                    or _fingerprint(request.get("offered_options")) != _fingerprint(offered)):
                request.update(status="offered", offered_options=offered,
                               selected_option_id=None, confirmation=None)
                parts.append(_offer_text(offered))
                log(request, "reply", asked="choose_and_confirm")
                cp2.append({"request_id": request["id"], "intent": request["intent"],
                            "decision": "options", "rule_id": decision.rule_id,
                            "options": [o["option_id"] for o in offered]})
                break
            ids = [o["option_id"] for o in offered]
            selected = request.get("selected_option_id")
            if selected not in ids:
                selected = request["selected_option_id"] = None
            confirmation = request.get("confirmation")
            if confirmation == "no":
                close(request, "customer_declined", "customer_declined_option",
                      "Understood. I won't make any changes for this request.")
                continue
            if selected is None and len(ids) == 1 and confirmation == "yes":
                selected = request["selected_option_id"] = ids[0]
            if selected and confirmation == "yes":
                step = "authorize"
                log(request, step)
                break
            if selected:
                chosen = next(o for o in offered if o["option_id"] == selected)
                request["confirmation"] = None
                parts.append(f"Just to confirm: {chosen['summary']} Shall I go ahead? (yes/no)")
                log(request, "reply", asked="confirm")
                break
            parts.append(_offer_text(offered))
            log(request, "reply", asked="choose_and_confirm")
            break

        return {"requests": requests, "next_step": step, "next_tool": tool,
                "response_parts": parts, "cp1_events": cp1, "cp2_events": cp2}

    # ----------------------------------------------------------------- query
    def query(state: ChatState) -> dict:
        tool = state["next_tool"]
        if tool == "verify_customer":
            claims = state.get("identity_claims") or {}
            method = policy.identity_method(claims)
            if method is None:
                raise RuntimeError("verify_customer called without a complete identity method")
            result = customer_lookup({k: claims[k] for k in method})
            return {"identity_status": result["status"],
                    "customer_id": result.get("customer_id"),
                    "member_level": result.get("member_level")}
        requests = deepcopy(state["requests"])
        index = _active_index(requests)
        if index is None:
            raise RuntimeError("query called without an active request")
        request = requests[index]
        if tool == "lookup_order":
            request["order_result"] = order_lookup(state["customer_id"], request["order_id"])
        elif tool == "check_coupon":
            request["coupon_result"] = coupon_lookup(state["customer_id"], request["coupon_code"])
        else:
            raise ValueError(f"unknown query tool: {tool}")
        return {"requests": requests}

    # ------------------------------------------------------------- authorize
    def authorize(state: ChatState, config: RunnableConfig) -> dict:
        thread_id = config["configurable"]["thread_id"]
        requests = deepcopy(state["requests"])
        index = _active_index(requests)
        if index is None:
            raise RuntimeError("authorize called without an active request")
        request = requests[index]
        option = next(o for o in request["offered_options"]
                      if o["option_id"] == request["selected_option_id"])
        parts = list(state.get("response_parts") or [])
        cp2 = list(state.get("cp2_events") or [])
        auth_request = AuthorizationRequest(
            conversation_id=thread_id,
            request_key=f"{thread_id}:{request['id']}:action",
            intent=request["intent"],
            customer_id=state["customer_id"],
            order_id=request.get("order_id"),
            item_index=request.get("item_index"),
            option={k: option[k] for k in ("option_id", "action", "payload")},
            confirmed=request.get("confirmation") == "yes",
            customer_facts=request.get("customer_facts") or {},
            coupon_result=request.get("coupon_result"),
        )
        try:
            result = policy_gate.authorize(auth_request)
        except ValueError:
            result = None
        if result is not None and result["outcome"] == "simulated":
            request["status"] = "resolved"
            parts.append(f"Done. {option['summary']} {SIMULATION_NOTE}")
        else:
            request["status"] = "handoff"
            parts.append("I couldn't complete that safely. " + HANDOFF_MESSAGE)
            if result is None:
                policy_gate.record_outcome(
                    outcome="handoff", conversation_id=thread_id,
                    request_id=f"{thread_id}:{request['id']}:final", intent=request["intent"],
                    reason_code="invalid_option", customer_id=state["customer_id"],
                    order_id=request.get("order_id"),
                )
        request["outcome"] = result
        cp2.append({"request_id": request["id"], "intent": request["intent"],
                    "decision": "authorize", "option_id": option["option_id"],
                    "gate_outcome": result["outcome"] if result else "handoff",
                    "reason_code": result["reason_code"] if result else "invalid_option",
                    "rule_id": result["policy_rule_id"] if result else None})
        return {"requests": requests, "response_parts": parts, "cp2_events": cp2}

    # ----------------------------------------------------------------- reply
    def reply(state: ChatState) -> dict:
        message = "\n".join(state.get("response_parts") or [GREETING])
        return {"messages": [AIMessage(content=message)], "reply_text": message}

    builder = StateGraph(ChatState)
    builder.add_node("understand", understand)
    builder.add_node("route", route)
    builder.add_node("query", query)
    builder.add_node("authorize", authorize)
    builder.add_node("reply", reply)
    builder.add_edge(START, "understand")
    builder.add_edge("understand", "route")
    builder.add_conditional_edges("route", lambda state: state["next_step"],
                                  {"reply": "reply", "query": "query", "authorize": "authorize"})
    builder.add_edge("query", "route")
    builder.add_edge("authorize", "route")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=checkpointer)
