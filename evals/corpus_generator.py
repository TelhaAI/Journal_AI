"""Synthetic Look Back corpus generator (plan §6.3): 4–6 months of dated fictional entries with planted,
machine-checkable features and stored ground truth. Randomized surface text, deterministic per seed."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

WORD_CHOICES = ["allowed", "should", "enough", "selfish", "responsible", "safe", "waste", "supposed to"]
SUBJECT_CHOICES = ["my sister's house", "the garden", "Dana's texts", "the night shift", "my father's car"]
DISAPPEARING_CHOICES = ["the promotion", "the apartment hunt", "the marathon", "the lawsuit", "the wedding plans"]
NOISE = ["Tried a new lentil recipe; too much cumin.", "The car needs a brake job, the shop said Thursday.",
         "Rained all afternoon, the gutter on the north side is loose.", "Finished the crossword in one sitting.",
         "The pharmacy moved to the strip mall by the highway."]
FILLER = [
    "Long day. Nothing much to add.", "Slept badly and it showed.", "Walked the dog twice, which is a record.",
    "Meeting ran over. Ate lunch at my desk.", "Called Mom. She's fine. She says she's fine.",
    "Quiet evening. Read for an hour.", "Traffic was awful and I didn't mind.", "Skipped the gym again.",
    "Made soup. It was fine.", "Couldn't settle tonight.", "Work was work.", "Bright cold morning.",
]
WANT_LINES = ["What I want is more time.", "I keep saying I want more time for myself.",
              "More time. That's the whole wish."]
CHOICE_LINES = ["Took on another project today because nobody else would.", "Said yes to another project.",
                "Signed up for one more project. I know."]


@dataclass
class GroundTruth:
    recurring_word: str
    recurring_word_count: int
    recurring_word_entry_ids: list[str]
    recurring_subject: str
    recurring_subject_entry_ids: list[str]
    disappearing: str
    disappearing_last_entry_id: str
    contradiction_pairs: list[tuple[str, str]]
    certainty_then_id: str
    certainty_now_id: str
    noise_entry_ids: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Corpus:
    seed: int
    entries: list[dict]  # {id, body, created_at_local}
    truth: GroundTruth


def _use_word(word: str, rng: random.Random) -> str:
    templates = {
        "allowed": ["I don't know if I'm {w} to want that.", "Am I {w} to say no?", "Nobody said I wasn't {w}."],
        "should": ["I {w} call her back.", "I probably {w} have said something.", "I {w} be grateful, I know."],
        "enough": ["It wasn't {w}.", "I never feel like it's {w}.", "Is this {w}?"],
        "selfish": ["Feels {w} to even write that.", "Am I being {w}?", "Everyone would call it {w}."],
        "responsible": ["Staying is the {w} thing, they'd say.", "I'm the {w} one, apparently.", "Someone has to be {w}."],
        "safe": ["At least it's {w}.", "I chose {w} again.", "Nothing about it feels {w}."],
        "waste": ["What a {w} of a Saturday.", "I don't want to {w} another year.", "It felt like a {w}."],
        "supposed to": ["I'm {w} be over this by now.", "This is what I'm {w} want.", "Who decided what I'm {w} do?"],
    }
    return rng.choice(templates[word]).format(w=word)


def generate_corpus(seed: int, months: int | None = None) -> Corpus:
    rng = random.Random(seed)
    months = months or rng.randint(4, 6)
    start = datetime(2026, 3, 1, 21, 0)
    n_entries = rng.randint(28, 40)
    days = sorted(rng.sample(range(months * 30), n_entries))
    dates = [start + timedelta(days=d) for d in days]
    bodies: list[list[str]] = [[rng.choice(FILLER)] for _ in dates]

    word = rng.choice(WORD_CHOICES)
    subject = rng.choice(SUBJECT_CHOICES)
    disappearing = rng.choice(DISAPPEARING_CHOICES)

    # recurring word: 7 occurrences across >= 4 entries
    word_slots = rng.sample(range(n_entries), rng.randint(4, 6))
    occurrences = 0
    for i in word_slots:
        bodies[i].append(_use_word(word, rng))
        occurrences += 1
    while occurrences < 7:
        i = rng.choice(word_slots)
        bodies[i].append(_use_word(word, rng))
        occurrences += 1

    # recurring subject: 6 entries spread across the range
    subj_slots = sorted(rng.sample(range(n_entries), 6))
    for i in subj_slots:
        bodies[i].append(rng.choice([f"Spent the evening at {subject}.", f"{subject.capitalize()} again today.",
                                     f"Thinking about {subject} more than I'd like."]))

    # disappearing subject: months 1–2 only
    early = [i for i, d in enumerate(dates) if (d - start).days < 60]
    dis_slots = sorted(rng.sample(early, min(4, len(early))))
    for i in dis_slots:
        bodies[i].append(rng.choice([f"{disappearing.capitalize()} came up again.", f"Still no word on {disappearing}.",
                                     f"Talked about {disappearing} at dinner."]))

    # contradiction: want vs choice, x3, in separate entries
    free = [i for i in range(n_entries) if i not in word_slots and i not in dis_slots]
    pairs = []
    for k in range(3):
        a, b = rng.sample(free, 2)
        bodies[a].append(WANT_LINES[k])
        bodies[b].append(CHOICE_LINES[k])
        pairs.append((a, b))
        free = [i for i in free if i not in (a, b)]

    # certainty shift: first month hedge, last month certainty
    first_month = [i for i, d in enumerate(dates) if (d - start).days < 30]
    last_month = [i for i, d in enumerate(dates) if (d - start).days >= (months - 1) * 30]
    then_i = rng.choice(first_month) if first_month else 0
    now_i = rng.choice(last_month) if last_month else n_entries - 1
    bodies[then_i].append("I don't know if I could ever leave.")
    bodies[now_i].append("I know I won't stay past spring.")

    # noise: 5 unrelated details, once or twice each
    noise_ids: dict[str, list[int]] = {}
    for line in NOISE:
        slots = rng.sample(range(n_entries), rng.randint(1, 2))
        for i in slots:
            bodies[i].append(line)
        noise_ids[line] = slots

    ids = [f"c{seed}-{i:02d}" for i in range(n_entries)]
    entries = [{"id": ids[i], "body": " ".join(bodies[i]), "created_at_local": dates[i]} for i in range(n_entries)]
    truth = GroundTruth(
        recurring_word=word, recurring_word_count=occurrences, recurring_word_entry_ids=[ids[i] for i in sorted(word_slots)],
        recurring_subject=subject, recurring_subject_entry_ids=[ids[i] for i in subj_slots],
        disappearing=disappearing, disappearing_last_entry_id=ids[dis_slots[-1]],
        contradiction_pairs=[(ids[a], ids[b]) for a, b in pairs],
        certainty_then_id=ids[then_i], certainty_now_id=ids[now_i],
        noise_entry_ids={k: [ids[i] for i in v] for k, v in noise_ids.items()},
    )
    return Corpus(seed=seed, entries=entries, truth=truth)


def noise_terms() -> list[str]:
    return ["lentil", "cumin", "brake", "gutter", "crossword", "pharmacy", "strip mall"]
