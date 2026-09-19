"""Errors raised by the client.

A refusal is not an exception in the usual sense. The engine understood the
request and declined it, and it told you what to do about that. The distinction
matters most to an agent: the difference between "change the arguments" and
"stop" is the difference between a self-correcting loop and an infinite one.
"""

from __future__ import annotations

from typing import Any, Literal

Retry = Literal["modify", "later", "never"]


class SemanticError(Exception):
    """Base class, so a caller can catch everything this library raises."""


class TransportError(SemanticError):
    """The engine could not be reached, or answered with something unparseable.

    This is a network or deployment problem, never a statement about the
    request. Retrying the same request is reasonable.
    """


class Unauthorized(SemanticError):
    """Credentials are missing or not recognised."""


class Refused(SemanticError):
    """The engine declined the request and said why.

    Attributes:
        code: Machine-readable reason. Branch on this, never on the text.
        reason: One sentence stating what was wrong.
        hint: What to do instead. For a fan-out refusal this names the metrics
            defined at the grain where the question is well defined.
        retry: One of "modify", "later" or "never". See :meth:`should_modify`,
            :meth:`should_wait` and :meth:`is_final`.
        status: The HTTP status that carried the refusal.
    """

    def __init__(
        self,
        code: str,
        reason: str,
        hint: str = "",
        retry: Retry = "never",
        status: int = 0,
    ) -> None:
        self.code = code
        self.reason = reason
        self.hint = hint
        self.retry: Retry = retry
        self.status = status
        super().__init__(self._render())

    def _render(self) -> str:
        text = f"{self.code}: {self.reason}"
        if self.hint:
            text += f"\n  hint: {self.hint}"
        text += f"\n  retry: {self.retry}"
        return text

    @classmethod
    def from_payload(cls, payload: dict[str, Any], status: int) -> "Refused":
        return cls(
            code=str(payload.get("code", "unknown")),
            reason=str(payload.get("reason", "")),
            hint=str(payload.get("hint", "")),
            retry=payload.get("retry", "never"),
            status=status,
        )

    def should_modify(self) -> bool:
        """The request is answerable, but not as written.

        Change the arguments and try again. Repeating it unchanged will not
        work. The hint usually names what to change.
        """
        return self.retry == "modify"

    def should_wait(self) -> bool:
        """Nothing about the request is wrong.

        Something outside it failed, such as the policy source being
        unreachable. The same request may succeed shortly.
        """
        return self.retry == "later"

    def is_final(self) -> bool:
        """No version of this request from this caller will succeed.

        Usually a denial. Say so rather than substituting a different metric
        that answers a different question.
        """
        return self.retry == "never"
