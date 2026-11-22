"""Trust: the verifier decides what becomes a fact; the data steward corrects the record."""
from gtl.trust.checks import CHECKS
from gtl.trust.steward import DataStewardAgent
from gtl.trust.verifier import VerifierAgent

AGENTS = [VerifierAgent(), DataStewardAgent()]

__all__ = ["AGENTS", "CHECKS", "DataStewardAgent", "VerifierAgent"]
