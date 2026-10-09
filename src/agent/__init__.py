"""Codebase Welcome Guide agent.

A stateless request/response pipeline: validate a GitHub URL, read the
repository live from GitHub's public endpoints, assemble a bounded context, ask
Amazon Nova Lite one question, return one assistant message.

Module map (see the design's Project Layout section):

    config.py           every limit and budget as a named constant
    models.py           frozen dataclasses shared across the pipeline
    errors.py           ErrorCode, AgentError, the copy table and render()
    steps.py            step vocabulary, step timing, deadline guard
    handler.py          Lambda entry point, guards, the single error boundary
"""
