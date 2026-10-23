"""Explicit live example; importing this module never calls paid APIs."""

import argparse

from agentrun import Agent as DirectAgent
from agentrun import SupervisedAgent as Agent
from agentrun import SupervisorLimits
from agentrun.console import run_console
from AgentBrowser.demo import load_environment
from AgentBrowser.logs import print_run_summary

GOAL = (
    "Accept cookies if exists. "
    "Type Volvo XC60, select the model XC60 and press Aceptar in the model panel. "
    "In Provincia type Valladolid, select Valladolid and press Aceptar in the provincias panel. "
    "Filter maximum price to 50.000 €. "
    "Click Show Results and wait for the results panel. "
    "For the first 10 distinct results in the observed order, including further pages when needed: "
    "open the advertisement, "
    "add to favourites "
    "Verify the submission before marking that advertisement completed. "
    "Go back to the results page and continue with the next advertisement not yet completed. "
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", action="store_true", help="Comparar con Jev sin supervisor.")
    parser.add_argument("--close-tab", action="store_true", help="Cerrar la pestaña al terminar.")
    parser.add_argument("--no-input", action="store_true", help="Guardar la pausa sin pedir una respuesta por consola.")
    parser.add_argument("--no-trace", action="store_true", help="Desactivar las trazas locales de esta sesión.")
    parser.add_argument("--trace-dir", help="Directorio raíz para las trazas locales completas.")
    args = parser.parse_args(argv)
    load_environment()
    if args.direct:
        options = {"max_actions": 300}
    else:
        options = {}
        options["limits"] = SupervisorLimits(max_actions=300)
    if args.close_tab:
        options["close_tab_on_exit"] = True
    if args.no_trace:
        options["trace"] = False
    elif args.trace_dir:
        from AgentBrowser.observability import TraceConfig

        options["trace"] = TraceConfig(directory=args.trace_dir)
    agent_type = DirectAgent if args.direct else Agent
    try:
        with agent_type("https://www.coches.net", GOAL, **options) as agent:
            final = None
            result = None
            if args.direct:
                for final in agent.run():
                    pass
            else:
                result = run_console(agent, no_input=args.no_input)
                final = result.state
            if final is not None:
                summary_state = dict(final)
                if result and result.question_shown:
                    summary_state["question"] = ""
                print_run_summary(summary_state)
            if result:
                return result.exit_code
    except KeyboardInterrupt:
        print("Ejecución interrumpida.")
        return 130
    if final is None:
        return 1
    return 0 if final["status"] == "done" and final.get("verification", {}).get("status") == "satisfied" else 2


if __name__ == "__main__":
    raise SystemExit(main())
