"""The planner: a linear program over the remaining pack-out hours."""
from gtl.plan.agent import AGENTS, PlannerAgent
from gtl.plan.inputs import build_plan_input
from gtl.plan.solver import plan, what_if

# Executed actions of these kinds change what the planner expects to arrive, so they
# make a new forecast due (see plan/inputs.py: executed pick-ups and offers become arrivals).
PLANNED_ACTIONS = ("accept_offer", "place_order", "request_pickup")

__all__ = ["AGENTS", "PLANNED_ACTIONS", "PlannerAgent", "build_plan_input", "plan", "what_if"]
