"""Synthetic support tickets that change from day to day (experiment 8 and the example).

Each ticket has an id, a true category (billing, bug or feature) and a text. A
:class:`TicketStream` produces one list per day: some tickets are new, some are
edited in ways that don't change what they are about (typos, a greeting, a
thank-you, a timestamp) and some are edited in ways that do (the customer's actual
problem changes).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

CATEGORIES = ("billing", "bug", "feature")

_PROBLEMS: dict[str, tuple[str, ...]] = {
    "billing": (
        "I was charged twice for my subscription this month and need a refund.",
        "My invoice shows the wrong price for the team plan.",
        "The payment failed but I was still billed for the annual subscription.",
        "Please refund the charge for the plan I cancelled last week.",
        "I was billed in dollars instead of rupees on my last invoice.",
    ),
    "bug": (
        "The app crashes every time I open the settings page.",
        "Uploading a photo fails with an error about the file size.",
        "The dashboard freezes when I switch to the monthly view.",
        "Search is broken and shows an error for any query with a hyphen.",
        "The export button doesn't load anything on Safari.",
    ),
    "feature": (
        "I would like an option to export reports as PDF.",
        "Please add support for dark mode in the mobile app.",
        "It would be great to have a feature for scheduling posts.",
        "We wish there was an option to share dashboards with guests.",
        "Could you add support for two-factor login with a hardware key?",
    ),
}
# Problems that differ by a few words but belong to different categories, so an edit
# between them is meaningful yet leaves the ticket's text very similar.
_SUBTLE: tuple[dict[str, str], ...] = (
    {
        "billing": "I was charged twice when I opened the reports page.",
        "bug": "The app crashed twice when I opened the reports page.",
        "feature": "I would like an option to pin the reports page.",
    },
    {
        "billing": "The invoice for the team plan shows the wrong price.",
        "bug": "The page for the team plan shows an error.",
        "feature": "Please add support for the team plan in the mobile app.",
    },
    {
        "billing": "My payment for the upgrade failed but I was billed.",
        "bug": "My upload for the upgrade fails with an error.",
        "feature": "Please add an option to preview the upgrade.",
    },
)
_OPENINGS = ("Hi team,", "Hello,", "Good morning,", "Hey,", "Hi there,")
_CONTEXT = (
    "I have been using the product for about a year.",
    "This is for our company account.",
    "We are a small team of five people.",
    "I am on the latest version.",
    "This started after the last update.",
)
_PLANS = ("Free", "Starter", "Team", "Business", "Enterprise")
_REGIONS = ("India", "Europe", "US East", "Singapore", "Brazil")
_BROWSERS = ("Chrome 141", "Firefox 140", "Safari 19", "Edge 141")
_AGENTS = ("Priya", "Arjun", "Meera", "Rahul", "Ananya", "Vikram")
_NOTE_ACTIONS = (
    "escalated to tier two",
    "asked the customer for screenshots",
    "linked this to an older ticket",
    "changed the priority to normal",
    "copied in the account owner",
)
_QUEUES = ("general", "priority", "enterprise", "follow-up")
_CLOSINGS = ("Thanks.", "Thank you!", "Regards.", "Cheers.", "Thanks in advance.")


@dataclass
class Ticket:
    id: str
    category: str
    opening: str
    problem: str
    context: str
    closing: str
    details: str = ""
    subtle: int | None = None  # index into _SUBTLE when the problem comes from there

    @property
    def text(self) -> str:
        return f"{self.opening} {self.problem} {self.context} {self.details} {self.closing}"

    def row(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text, "category": self.category}


def _typo(sentence: str, rng: random.Random) -> str:
    words = sentence.split()
    i = rng.randrange(len(words))
    w = words[i]
    if len(w) > 3:
        j = rng.randrange(1, len(w) - 1)
        words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2 :]
    return " ".join(words)


@dataclass
class TicketStream:
    """Day-by-day ticket lists with known kinds of edits."""

    size: int = 120
    new_rate: float = 0.15
    trivial_rate: float = 0.25
    meaningful_rate: float = 0.05
    subtle_share: float = 0.4
    # "small": typos, greetings, closings; "notes": support staff rewrite the ticket's
    # internal-notes line, a large edit that still doesn't change what it is about.
    trivial_style: str = "small"
    seed: int = 0
    tickets: list[Ticket] = field(default_factory=list)
    _next: int = 0

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.tickets = [self._new() for _ in range(self.size)]

    def _new(self) -> Ticket:
        self._next += 1
        category = self.rng.choice(CATEGORIES)
        subtle = self.rng.randrange(len(_SUBTLE)) if self.rng.random() < self.subtle_share else None
        problem = (
            _SUBTLE[subtle][category]
            if subtle is not None
            else self.rng.choice(_PROBLEMS[category])
        )
        return Ticket(
            id=f"T{self._next:04d}",
            category=category,
            subtle=subtle,
            opening=self.rng.choice(_OPENINGS),
            problem=problem,
            context=self.rng.choice(_CONTEXT),
            closing=self.rng.choice(_CLOSINGS),
            details=(
                f"Account {self.rng.randrange(1000, 9999)}, plan {self.rng.choice(_PLANS)}, "
                f"region {self.rng.choice(_REGIONS)}, browser {self.rng.choice(_BROWSERS)}."
            ),
        )

    def _trivial(self, t: Ticket) -> None:
        if self.trivial_style == "notes":
            t.closing = (
                f"Note: {self.rng.choice(_AGENTS)} {self.rng.choice(_NOTE_ACTIONS)} "
                f"at {self.rng.randrange(8, 20)}:{self.rng.randrange(60):02d}."
            )
            return
        kind = self.rng.randrange(4)
        if kind == 0:
            t.opening = self.rng.choice([o for o in _OPENINGS if o != t.opening])
        elif kind == 1:
            t.closing = self.rng.choice([c for c in _CLOSINGS if c != t.closing])
        elif kind == 2:
            t.problem = _typo(t.problem, self.rng)
        else:
            t.context = self.rng.choice([c for c in _CONTEXT if c != t.context])

    def _meaningful(self, t: Ticket) -> None:
        t.category = self.rng.choice([c for c in CATEGORIES if c != t.category])
        if t.subtle is not None:
            t.problem = _SUBTLE[t.subtle][t.category]  # a few words change
        else:
            t.problem = self.rng.choice(_PROBLEMS[t.category])

    def today(self) -> list[dict[str, str]]:
        """The current tickets as rows (``id``, ``text``, ``category``)."""
        return [t.row() for t in self.tickets]

    def advance(self) -> dict[str, int]:
        """Move to the next day; returns how many tickets of each kind of change."""
        counts = {"new": 0, "trivial": 0, "meaningful": 0}
        for i, t in enumerate(self.tickets):
            roll = self.rng.random()
            if roll < self.new_rate:
                self.tickets[i] = self._new()
                counts["new"] += 1
            elif roll < self.new_rate + self.trivial_rate:
                self._trivial(t)
                counts["trivial"] += 1
            elif roll < self.new_rate + self.trivial_rate + self.meaningful_rate:
                self._meaningful(t)
                counts["meaningful"] += 1
        return counts
