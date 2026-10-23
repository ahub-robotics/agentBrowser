"""uv run --env-file .env python examples/run.py --url URL --goal 'A narrow goal'"""

import argparse

from AgentBrowser import Agent

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--goal", action="append", required=True, help="Repeat for an ordered list of goals.")
parser.add_argument("--close-tab", action="store_true", help="Close the working tab when the run ends.")
args = parser.parse_args()

options = {"close_tab_on_exit": True} if args.close_tab else {}
with Agent(args.url, args.goal, **options) as agent:
    for state in agent.run():
        print(f"{state['elapsed_ms']:>5} ms  {len(state['history'])} actions  {state['status']}")
    print(state["page"]["url"])
