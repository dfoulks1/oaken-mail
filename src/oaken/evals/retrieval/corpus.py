# SPDX-License-Identifier: Apache-2.0
"""A seeded synthetic mailbox with facts planted in it, and questions whose answers are those facts.

Nothing here is real mail: addresses are on reserved example domains and every word comes from the
lists below. The corpus is built so that each retriever has something it is bad at:

* ``lexical``     the question uses the words the message uses: both retrievers should find it;
* ``paraphrase``  the question uses *synonyms* of the message's words, so keyword search matches
                  only distractors that happen to use the question's words;
* ``identifier``  the question carries a rare token (an order or ticket number) in a mailbox full
                  of near-identical receipts, which meaning alone cannot tell apart;
* ``thread``      the fact is in a reply, among replies;
* ``unanswerable`` nothing in the mailbox answers it (the right answer is "not enough evidence").

Everything is a pure function of the seed, so a report can name the seed it was made from.
"""

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

KINDS: Final = ("lexical", "paraphrase", "identifier", "thread", "unanswerable")

# Words that mean the same thing to the stand-in embedder (and to a person). A paraphrase question
# uses one word of a group, the fact uses another.
SYNONYMS: Final[tuple[tuple[str, ...], ...]] = (
    ("invoice", "bill", "statement"),
    ("meeting", "call", "catchup"),
    ("deadline", "cutoff", "due"),
    ("budget", "funding", "allocation"),
    ("laptop", "notebook", "computer"),
    ("refund", "reimbursement", "payback"),
    ("flight", "airfare", "plane"),
    ("hotel", "lodging", "accommodation"),
    ("manager", "supervisor", "boss"),
    ("password", "credentials", "login"),
    ("contract", "agreement", "deal"),
    ("shipment", "delivery", "parcel"),
    ("office", "workplace", "headquarters"),
    ("salary", "pay", "compensation"),
    ("approved", "authorized", "sanctioned"),
    ("replacement", "refresh", "upgrade"),
)
_SYN = {w: g for g in SYNONYMS for w in g}

_NAMES: Final = ("alex", "sam", "jordan", "casey", "robin", "taylor", "morgan", "riley", "jules")
_FILLER: Final = (
    "please let me know if anything changes on your side and we will adjust the plan",
    "the team has been working through the backlog and most items are now closed",
    "thanks again for the quick turnaround on the last round of comments",
    "i have attached nothing this time but the notes are in the shared folder",
    "we should keep the next steps small so that nobody is blocked for long",
    "the numbers look reasonable to me although finance will want to double check them",
    "nothing urgent here just keeping everyone in the loop before the weekend",
    "remember that the building is closed on public holidays and the lifts are slow",
    "can you take another look when you have a moment and tell me what you think",
    "the vendor promised an update soon but we have heard that before",
    "this is a reminder that expenses are due at the end of the month as usual",
    "good news on the hiring front with two more interviews booked for next week",
    "the weather has been terrible so many people are working from home",
    "i will be out on friday so please send anything important before then",
    "we are still waiting to hear back from legal about the standard wording",
)
# Everyday words in the *question's* vocabulary, scattered through filler so that a keyword search
# for a paraphrase question finds plenty of messages that are not the answer.
_DECOYS: Final = tuple(w for g in SYNONYMS for w in g)
# Everyday projects and rooms, used freely by the filler mail. The planted facts use names from
# _CODENAMES instead, which the filler never mentions, so each fact has exactly one right message.
_PROJECTS: Final = ("Atlas", "Beacon", "Compass", "Delta", "Echo", "Foxtrot", "Granite", "Harbor")
_ROOMS: Final = ("Aspen", "Birch", "Cedar", "Dune", "Elm", "Fir")
_CODENAMES: Final = (
    "Falcon", "Orchid", "Juniper", "Willow", "Cobalt", "Marlin", "Ember", "Quartz", "Saffron",
    "Tundra", "Viper", "Zephyr", "Basalt", "Cinder", "Drizzle", "Fennel", "Gossamer", "Hollow",
    "Indigo", "Jasper", "Kestrel", "Lantern", "Mistral", "Nimbus",
)  # fmt: skip
_SECRET_ROOMS: Final = ("Obsidian", "Pavilion", "Quarry", "Rotunda", "Solarium", "Terrace")
_SHOPS: Final = ("Brightwell", "Corvid", "Harbor", "Lumen", "Marrow", "Nettle")
_DAYS: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
_MONTHS: Final = ("March", "April", "May", "June", "July", "September", "October")


@dataclass(frozen=True)
class CorpusMessage:
    provider_id: str
    raw: bytes
    category: str  # newsletter | receipt | scheduling | thread | personal
    planted: str | None = None  # the id of the fact this message holds, if any


@dataclass(frozen=True)
class Question:
    id: str
    kind: str
    text: str
    relevant: tuple[str, ...]  # provider ids of the messages that answer it (empty: unanswerable)
    fact: str = ""  # the sentence that answers it, for scoring a quote


@dataclass
class Corpus:
    seed: int
    messages: list[CorpusMessage] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)

    def answerable(self) -> list[Question]:
        return [q for q in self.questions if q.relevant]


def _sentences(rng: random.Random, n: int, decoys: int = 0) -> str:
    out = [rng.choice(_FILLER) for _ in range(n)]
    for _ in range(decoys):  # a decoy sentence uses a handful of everyday words
        words = rng.sample(_DECOYS, 3)
        out.insert(
            rng.randrange(len(out) + 1),
            f"someone asked about the {words[0]} and the {words[1]} and the {words[2]} again",
        )
    return ". ".join(s.capitalize() for s in out) + "."


def _stamp(i: int) -> str:
    when = datetime(2024, 1, 1, 8, tzinfo=UTC) + timedelta(minutes=i * 53)
    return f"{when:%a, %d %b %Y %H:%M:%S +0000}"


def _mail(
    i: int, sender: str, subject: str, body: str, *, seed: int, reply_to: int | None = None,
    list_id: str | None = None,
) -> bytes:  # fmt: skip
    rcpt = _NAMES[(i + 3) % len(_NAMES)]
    headers = [
        f"From: {sender.title()} <{sender}@example.com>",
        f"To: {rcpt.title()} <{rcpt}@example.org>",
        f"Subject: {subject}",
        f"Date: {_stamp(i)}",
        f"Message-ID: <e{i}.{seed}@example.com>",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if reply_to is not None:
        headers += [f"In-Reply-To: <e{reply_to}.{seed}@example.com>",
                    f"References: <e{reply_to}.{seed}@example.com>"]  # fmt: skip
    if list_id:
        headers += [f"List-Id: <{list_id}.example.com>", "List-Unsubscribe: <mailto:u@example.com>"]
    return ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode()


def generate(count: int = 1000, *, seed: int = 0, per_kind: int = 12) -> Corpus:
    """``count`` messages (at least enough to hold the planted ones) and ``per_kind`` questions of
    each kind. Deterministic for a given ``(count, seed, per_kind)``."""
    rng = random.Random(seed)  # noqa: S311 (reproducible test data)
    corpus = Corpus(seed)
    planted: dict[int, tuple[str, str]] = {}  # message slot -> (fact id, sentence)

    # The facts first, so their slots are known; the rest of the mailbox is filler around them.
    slots = rng.sample(range(count), min(count, per_kind * 4 + 4))
    pool = iter(slots)
    facts: list[tuple[str, str, str, tuple[int, ...]]] = []  # (id, sentence, question, slots)

    def fact(kind: str, n: int, sentence: str, question: str, *at: int) -> None:
        facts.append((f"{kind}-{n}", sentence, question, at))

    used_orders: set[int] = set()
    used_groups: list[set[int]] = []
    for n in range(per_kind):
        project = _CODENAMES[n * 2 % len(_CODENAMES)]
        decided_project = _CODENAMES[(n * 2 + 1) % len(_CODENAMES)]
        room, day = _SECRET_ROOMS[n % len(_SECRET_ROOMS)], rng.choice(_DAYS)
        month, date, hour = rng.choice(_MONTHS), rng.randint(2, 28), rng.randint(9, 16)
        # lexical: the question repeats the message's own rare words
        fact("lexical", n,
             f"The {project} {room} planning session is on {day} {month} {date} at {hour}:00",
             f"When is the {project} {room} planning session?", next(pool))  # fmt: skip
        # paraphrase: the same four concepts as the question in other words, and no rare word to
        # lean on. Each fact gets its own four concepts, so no two questions mean the same thing.
        groups = rng.sample(range(len(SYNONYMS)), 4)
        while any(len(set(groups) & used) >= 3 for used in used_groups):
            groups = rng.sample(range(len(SYNONYMS)), 4)
        used_groups.append(set(groups))
        said = [rng.choice(SYNONYMS[g]) for g in groups]
        asked = [rng.choice([w for w in SYNONYMS[g] if w != said[i]]) for i, g in enumerate(groups)]
        amount = rng.randint(3, 90) * 1000
        fact("paraphrase", n,
             f"The {said[0]} and the {said[1]} {said[2]} were {said[3]} at {amount} dollars",
             f"How much was set aside for the {asked[0]} and the {asked[1]} {asked[2]} {asked[3]}?",
             next(pool))  # fmt: skip
        # identifier: one receipt among many, told apart only by its number
        order = rng.randint(10000, 99999)
        while order in used_orders:
            order = rng.randint(10000, 99999)
        used_orders.add(order)
        shop = rng.choice(_SHOPS)
        code = f"{order}-{rng.choice('QXZ')}{rng.choice('QXZ')}"
        fact("identifier", n, f"Order {code} from {shop} was shipped via courier",
             f"What happened to order {order}?", next(pool))  # fmt: skip
        # thread: the answer is a reply a few messages into a conversation
        decided = f"{day} {month} {date}"
        decision = (
            f"Decision: we will ship the {decided_project} release on {decided} after the freeze"
        )
        question = f"What was decided about shipping the {decided_project} release?"
        fact("thread", n, decision, question, next(pool))

    topics = (
        "telescope",
        "warranty",
        "kayak",
        "mortgage",
        "passport",
        "orchestra",
        "vaccine",
        "harpsichord",
    )
    for n in range(per_kind):  # nothing in the mailbox mentions these
        corpus.questions.append(
            Question(
                f"unanswerable-{n}",
                "unanswerable",
                f"What is the {topics[n % len(topics)]} {rng.choice(('number', 'price', 'date'))}?",
                (),
            )  # fmt: skip
        )

    thread_of: dict[int, int] = {}  # slot -> the slot it replies to
    chain: list[int] = []
    for i in range(count):
        if rng.random() < 0.3 and chain:
            thread_of[i] = chain[-1]
        chain = [*chain, i][-3:] if rng.random() < 0.85 else [i]

    for fid, sentence, question, at in facts:
        kind = fid.split("-")[0]
        planted[at[0]] = (fid, sentence)
        corpus.questions.append(Question(fid, kind, question, (f"e{at[0]}",), sentence))

    for i in range(count):
        roll = rng.random()
        sender = rng.choice(_NAMES)
        plant = planted.get(i)
        category = "personal"
        subject: str
        body_paras: list[str]
        list_id = None
        if plant is not None and plant[0].startswith("identifier"):
            category = "receipt"
        elif plant is not None and plant[0].startswith("thread"):
            category = "thread"
        elif roll < 0.25:
            category = "newsletter"
        elif roll < 0.4:
            category = "receipt"
        elif roll < 0.55:
            category = "scheduling"
        elif roll < 0.8:
            category = "thread"
        if category == "newsletter":
            subject = f"{rng.choice(_SHOPS)} weekly digest {i}"
            body_paras = [_sentences(rng, rng.randint(5, 9)) for _ in range(rng.randint(6, 12))]
            list_id = f"digest{rng.randint(1, 5)}"
        elif category == "receipt":
            subject = f"Your order {rng.randint(100000, 999999)} receipt"
            order = rng.randint(100000, 999999)
            body_paras = [
                f"Thanks for shopping with {rng.choice(_SHOPS)}. Order {order} total "
                f"{rng.randint(5, 400)} dollars was shipped via courier. " + _sentences(rng, 2)
            ]
        elif category == "scheduling":
            subject = f"Planning {rng.choice(_PROJECTS)} {rng.choice(_ROOMS)} {rng.choice(_DAYS)}"
            body_paras = [
                f"The {rng.choice(_PROJECTS)} {rng.choice(_ROOMS)} planning session is on "
                f"{rng.choice(_DAYS)} {rng.choice(_MONTHS)} {rng.randint(2, 28)} at "
                f"{rng.randint(9, 16)}:00. " + _sentences(rng, 3)
            ]
        else:
            subject = (
                f"{'Re: ' if i in thread_of else ''}{rng.choice(_PROJECTS)} release notes {i % 17}"
            )
            body_paras = [_sentences(rng, rng.randint(3, 7)) for _ in range(rng.randint(2, 4))]
        if plant is not None:
            if plant[0].startswith("paraphrase"):
                category = "personal"
            body_paras.insert(rng.randrange(len(body_paras) + 1), plant[1] + ".")
        if rng.random() < 0.35:  # a decoy: a sentence in the question's vocabulary, not an answer
            words = rng.sample(_DECOYS, 3)
            body_paras.append(
                f"Someone asked about the {words[0]} and the {words[1]} and the {words[2]} again."
            )
        body = "\n\n".join(body_paras)
        if category == "thread" and i in thread_of:
            body += "\n\n> " + _sentences(rng, 2, decoys=0).replace(". ", ".\n> ")
        corpus.messages.append(
            CorpusMessage(
                f"e{i}",
                _mail(
                    i, sender, subject, body, seed=seed, reply_to=thread_of.get(i), list_id=list_id
                ),
                category,
                plant[0] if plant else None,
            )
        )
    return corpus
