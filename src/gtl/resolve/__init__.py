"""The resolver: candidate fixes, joint pricing, and the agent that requests them."""
from gtl.resolve.agent import ResolverAgent
from gtl.resolve.candidates import Candidate, Effect, candidates
from gtl.resolve.pricing import Priced, price

AGENTS = [ResolverAgent()]

__all__ = ["AGENTS", "Candidate", "Effect", "Priced", "ResolverAgent", "candidates", "price"]
