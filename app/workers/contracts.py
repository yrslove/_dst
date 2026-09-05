"""Compatibility exports; runtime_agent owns the canonical worker contract."""

from runtime_agent.gameworker.base import GameWorker, WorkerContext, WorkerReport

__all__ = ["GameWorker", "WorkerContext", "WorkerReport"]
