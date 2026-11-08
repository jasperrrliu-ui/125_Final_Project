"""Six-node LangGraph for the ABCD retail support demo.

The graph orchestrates existing tools. The language interpreter is injected:
tests use a scripted fake; a real model adapter can implement the same two
methods. Only the backend policy gate can write simulated action events.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Any, Callable, Protocol, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent.action_log.action_ledger import ActionProposal
from agent.action_log.policy_gate import PolicyGate, TrustedContext
from agent.retail_tools.check_coupon import check_coupon
from agent.retail_tools.read_tools import lookup_customer, lookup_order


INTENTS = {
    "return_size", "return_color", "return_stain",
    "promo_code_invalid", "promo_code_out_of_date",
    "manage_cancel", "manage_upgrade", "manage_downgrade",
}
RETURN_INTENTS = {"return_size", "return_color", "return_stain"}
PROMO_INTENTS = {"promo_code_invalid", "promo_code_out_of_date"}
ORDER_INTENTS = INTENTS - PROMO_INTENTS
SHIPPING_INTENTS = {"manage_cancel", "manage_upgrade", "manage_downgrade"}


class ChatState(TypedDict, total=False):
    messages: Annotated[list[Any], add_messages]
    account_identifier: dict[str, str] | None
    customer_id: str | None
    member_level: str | None
    customer_lookup_status: str | None
    requests: list[dict[str, Any]]
    next_step: str
    next_tool: str | None
    response_parts: list[str]
    reply_text: str
    proposal: dict[str, Any] | None
    cp1_events: list[dict[str, Any]]
    cp2_events: list[dict[str, Any]]


class Interpreter(Protocol):
    def extract(self, latest_message: str, state: ChatState) -> dict[str, Any]:
        """Return account_identifier, new requests, and/or active-task fields."""

    def propose(self, request: dict[str, Any], state: ChatState) -> dict[str, Any]:
        """Return action_type and payload, or action_type=None with a reason."""


def _active_index(requests: list[dict[str, Any]]) -> int | None:
    return next((i for i, request in enumerate(requests)
                 if request["status"] in {"open", "waiting"}), None)


def _add_event(state: ChatState, field: str, event: dict) -> list[dict]:
    return [*state.get(field, []), event]


def build_graph(
    interpreter: Interpreter,
    policy_gate: PolicyGate,
    checkpointer: Any,
    *,
    customer_lookup: Callable[..., dict] = lookup_customer,
    order_lookup: Callable[..., dict] = lookup_order,
    coupon_lookup: Callable[..., dict] = check_coupon,
    context_factory: Callable[[ChatState, dict], TrustedContext] | None = None,
):
    """Compile a graph. The caller owns the checkpointer's lifecycle."""

    def understand(state: ChatState) -> dict:
        latest = next((m.content for m in reversed(state["messages"])
                       if isinstance(m, HumanMessage)), "")
        parsed = interpreter.extract(str(latest), state)
        requests = deepcopy(state.get("requests", []))
        active_before = _active_index(requests)

        account = parsed.get("account_identifier") or state.get("account_identifier")
        account_changed = account != state.get("account_identifier")
        if account_changed:
            # Never carry one account's verified facts into another account.
            for request in requests:
                request.pop("order_result", None)
                request.pop("coupon_result", None)

        fields = parsed.get("fields") or {}
        if active_before is not None and isinstance(fields, dict):
            request = requests[active_before]
            for key in ("order_id", "item_index", "item_name", "coupon_code", "problem_detail"):
                if fields.get(key) is not None and fields[key] != request.get(key):
                    request[key] = fields[key]
                    if key == "order_id":
                        request.pop("order_result", None)
                    elif key == "coupon_code":
                        request.pop("coupon_result", None)
            request["status"] = "open"

        for item in parsed.get("requests", []):
            intent = item.get("intent")
            if intent not in INTENTS:
                continue
            requests.append({
                "id": f"r{len(requests) + 1}",
                "intent": intent,
                "order_id": item.get("order_id"),
                "item_index": item.get("item_index"),
                "item_name": item.get("item_name"),
                "coupon_code": item.get("coupon_code"),
                "problem_detail": item.get("problem_detail"),
                "status": "open",
            })
        return {
            "requests": requests,
            "account_identifier": account,
            "customer_id": None if account_changed else state.get("customer_id"),
            "member_level": None if account_changed else state.get("member_level"),
            "customer_lookup_status": None if account_changed else state.get("customer_lookup_status"),
            "response_parts": [],
            "proposal": None,
        }

    def route(state: ChatState) -> dict:
        requests = deepcopy(state.get("requests", []))
        index = _active_index(requests)
        step = "reply"
        tool = None
        addition = None
        if index is None:
            if not state.get("response_parts"):
                addition = "Please tell me what you need help with."
        else:
            request = requests[index]
            intent = request["intent"]
            if not state.get("account_identifier"):
                addition = "Please provide your full name or account ID so I can find your account."
            elif intent in RETURN_INTENTS and not request.get("problem_detail"):
                addition = "Please describe the size or color mismatch, or where the stain is."
            elif not state.get("customer_id"):
                if state.get("customer_lookup_status") in {"not_found", "multiple_candidates"}:
                    addition = "I could not identify a unique account. Please check and provide your account ID."
                    request["status"] = "waiting"
                else:
                    step, tool = "query", "lookup_customer"
            elif intent in PROMO_INTENTS:
                if not request.get("coupon_code"):
                    addition = "Please provide the original coupon code that failed at checkout."
                elif "coupon_result" not in request:
                    step, tool = "query", "check_coupon"
                else:
                    coupon = request["coupon_result"]
                    if coupon["status"] != "resolved":
                        request["status"] = "handoff"
                        addition = "I could not confirm this coupon checkout record. I will hand this case to a human agent."
                    else:
                        step = "propose"
            else:
                if not request.get("order_id"):
                    addition = "Please provide the order ID for this request."
                elif "order_result" not in request:
                    step, tool = "query", "lookup_order"
                else:
                    order = request["order_result"]
                    if order["status"] != "found":
                        request["status"] = "waiting"
                        addition = "I could not find that order under this account. Please check the order ID."
                    elif intent in SHIPPING_INTENTS and not order["facts"].get("shipping_status"):
                        request["status"] = "handoff"
                        addition = "The shipping status is unavailable, so I cannot apply the policy. I will hand this case to a human agent."
                    else:
                        items = order["facts"]["items"]
                        if intent in RETURN_INTENTS | {"manage_cancel"}:
                            if request.get("item_index") is None and request.get("item_name"):
                                name = request["item_name"].strip().casefold()
                                matches = [x for x in items if x["name"].replace("_", " ").casefold() == name]
                                if len(matches) == 1:
                                    request["item_index"] = matches[0]["item_index"]
                            if request.get("item_index") is None and len(items) == 1:
                                request["item_index"] = items[0]["item_index"]
                            if request.get("item_index") not in {x["item_index"] for x in items}:
                                addition = "Please tell me which item in the order this request concerns."
                            else:
                                step = "propose"
                        else:
                            step = "propose"

        event = {
            "request_id": requests[index]["id"] if index is not None else None,
            "intent": requests[index]["intent"] if index is not None else None,
            "next_step": step,
            "planned_tool": tool,
            "missing_or_problem": addition,
        }
        parts = [*state.get("response_parts", [])]
        if addition:
            parts.append(addition)
        return {"requests": requests, "next_step": step, "next_tool": tool,
                "response_parts": parts,
                "cp1_events": _add_event(state, "cp1_events", event)}

    def query(state: ChatState) -> dict:
        tool = state["next_tool"]
        if tool == "lookup_customer":
            identifier = state["account_identifier"]
            result = customer_lookup(identifier["type"], identifier["value"])
            return {
                "customer_id": result.get("customer_id"),
                "member_level": result.get("member_level"),
                "customer_lookup_status": result["status"],
            }
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

    def propose(state: ChatState) -> dict:
        requests = state["requests"]
        index = _active_index(requests)
        if index is None:
            raise RuntimeError("proposal called without an active request")
        request = requests[index]
        proposal = interpreter.propose(request, state)
        event = {"request_id": request["id"], "intent": request["intent"],
                 "proposal": proposal}
        return {"proposal": proposal,
                "cp2_events": _add_event(state, "cp2_events", event)}

    def authorize(state: ChatState, config: RunnableConfig) -> dict:
        requests = deepcopy(state["requests"])
        index = _active_index(requests)
        if index is None:
            raise RuntimeError("authorization called without an active request")
        request = requests[index]
        proposal = state["proposal"] or {}
        action = proposal.get("action_type")
        parts = [*state.get("response_parts", [])]
        gate_outcome = "handoff"
        reason_code = None
        policy_rule_id = None
        if action is None:
            request["status"] = "handoff"
            reason_code = "no_supported_proposal"
            parts.append("The available facts or policy do not support an automatic decision. I will hand this case to a human agent.")
        elif request["intent"] in PROMO_INTENTS:
            request["status"] = "handoff"
            reason_code = "coupon_issuance_not_implemented"
            parts.append("The coupon check is complete, but replacement coupon issuance is not authorized. I will hand this case to a human agent.")
        else:
            thread_id = config["configurable"]["thread_id"]
            action_proposal = ActionProposal(
                conversation_id=thread_id,
                request_id=f"{thread_id}:{request['id']}:{len(state.get('cp2_events', []))}",
                intent=request["intent"], action_type=action,
                customer_id=state["customer_id"], order_id=request["order_id"],
                payload=proposal.get("payload") or {},
            )
            context = (context_factory(state, request) if context_factory
                       else TrustedContext(state["customer_id"]))
            try:
                result = policy_gate.review_and_record(action_proposal, context)
            except ValueError:
                # A malformed model proposal never becomes a business action.
                request["status"] = "handoff"
                parts.append("I could not verify the proposed action parameters. I will hand this case to a human agent.")
                reason_code = "invalid_action_proposal"
                result = None
            if result is not None:
                request["outcome"] = result
                gate_outcome = result["outcome"]
                reason_code = result["reason_code"]
                policy_rule_id = result["policy_rule_id"]
                if result["outcome"] == "simulated":
                    request["status"] = "resolved"
                    parts.append(f"Simulated action recorded: {action}.")
                else:
                    request["status"] = "handoff"
                    parts.append(f"The policy check blocked this action ({result['reason_code']}). I will hand this case to a human agent.")
        cp2_events = deepcopy(state.get("cp2_events", []))
        cp2_events[-1].update({
            "gate_outcome": gate_outcome,
            "reason_code": reason_code,
            "policy_rule_id": policy_rule_id,
        })
        return {"requests": requests, "response_parts": parts,
                "proposal": None, "cp2_events": cp2_events}

    def reply(state: ChatState) -> dict:
        message = "\n".join(state.get("response_parts") or ["Please tell me what you need help with."])
        return {"messages": [AIMessage(content=message)], "reply_text": message}

    builder = StateGraph(ChatState)
    builder.add_node("understand", understand)
    builder.add_node("route", route)
    builder.add_node("query", query)
    builder.add_node("propose", propose)
    builder.add_node("authorize", authorize)
    builder.add_node("reply", reply)
    builder.add_edge(START, "understand")
    builder.add_edge("understand", "route")
    builder.add_conditional_edges("route", lambda state: state["next_step"],
                                  {"reply": "reply", "query": "query", "propose": "propose"})
    builder.add_edge("query", "route")
    builder.add_edge("propose", "authorize")
    builder.add_edge("authorize", "route")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=checkpointer)
