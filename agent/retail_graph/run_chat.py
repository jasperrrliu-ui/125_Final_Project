"""Local text chat entrypoint for the real LLM adapter (requires API key)."""

from __future__ import annotations

import argparse
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.action_log.action_ledger import ActionLedger
from agent.action_log.policy_gate import PolicyGate
from agent.retail_graph.graph import build_graph
from agent.retail_graph.llm_interpreter import OpenAIInterpreter


ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="ABCD retail agent local chat")
    parser.add_argument("--thread-id", default=None,
                        help="Reuse this ID to continue a previous conversation")
    args = parser.parse_args()
    thread_id = args.thread_id or f"demo-{uuid4().hex[:12]}"
    interpreter = OpenAIInterpreter()

    folder = ROOT / "outputs" / "retail_graph_demo"
    folder.mkdir(parents=True, exist_ok=True)
    ledger = ActionLedger(folder / "actions.sqlite")
    gate = PolicyGate(ledger)

    with SqliteSaver.from_conn_string(str(folder / "chat_state.sqlite")) as saver:
        graph = build_graph(interpreter, gate, saver)
        config = {"configurable": {"thread_id": thread_id}}
        print(f"Conversation ID: {thread_id}")
        print("Type /quit to leave. All business actions are simulations.")
        while True:
            try:
                customer_message = input("Customer> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if customer_message == "/quit":
                break
            if not customer_message:
                continue
            result = graph.invoke({"messages": [HumanMessage(content=customer_message)]}, config)
            print(f"Agent> {result['reply_text']}")


if __name__ == "__main__":
    main()
