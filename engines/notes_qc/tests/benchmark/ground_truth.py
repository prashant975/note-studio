"""The benchmark's seeded errors and what counts as finding one.

Accuracy cannot be improved without being measured, and this app had no way to
measure it: the only numbers anyone had came from a human grading a run by hand.
This module is the scoreboard — a set of deliberate errors with known answers,
so precision and recall become numbers a change can be judged against.

**Every error here is durable.** Not one depends on a figure that could have
been revised, a scheme that could have been renamed, or anything else that a
model's training cutoff could explain away. Constitutional articles, the years
of enacted laws, settled history, geography, arithmetic. If the checker misses
one of these, the checker is wrong — there is no "the notes are newer than me"
defence available. That is what makes the score meaningful.

Each entry carries:

``sentence``
    The wrong sentence, exactly as it appears in the built PDF.
``must_mention``
    Terms a finding has to contain to count as having caught THIS error. The
    match is deliberately generous on wording and strict on identity: a finding
    that says "Article 21 concerns life and personal liberty, not property"
    counts, one that vaguely says "check the articles" does not.
``category``
    Which of the framework's three categories the error belongs to.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SeededError:
    key: str
    category: str                     # factual | conceptual | language
    sentence: str
    must_mention: tuple[str, ...]
    note: str = ""

    def matched_by(self, text: str) -> bool:
        """True when ``text`` identifies this specific error.

        Every term in ``must_mention`` has to appear. One term alone is too
        cheap — "Article" appears in half the document — so identity is proved
        by the combination.
        """
        low = text.lower()
        return all(term.lower() in low for term in self.must_mention)


#: The wrong sentences that go into the benchmark PDF, with their answers.
SEEDED: tuple[SeededError, ...] = (
    # ---------------------------------------------------------- constitutional
    SeededError(
        key="art21-property",
        category="factual",
        sentence=("Article 21 of the Constitution guarantees the right to "
                  "property to every citizen of India."),
        must_mention=("21", "life"),
        note="Article 21 is life and personal liberty; property is Article 300A.",
    ),
    SeededError(
        key="73rd-year",
        category="factual",
        sentence=("The 73rd Amendment Act of 1994 gave constitutional status to "
                  "Panchayati Raj institutions across the country."),
        must_mention=("73rd", "1992"),
        note="The 73rd Amendment was 1992.",
    ),
    SeededError(
        key="rti-year",
        category="factual",
        sentence=("The Right to Information Act was passed by Parliament in "
                  "2003 and applies to every public authority."),
        must_mention=("right to information", "2005"),
        note="The RTI Act received assent in 2005.",
    ),
    SeededError(
        key="fundamental-duties",
        category="factual",
        sentence=("Fundamental Duties were added to the Constitution by the "
                  "44th Amendment Act on the recommendation of the Swaran "
                  "Singh Committee."),
        must_mention=("42nd", "duties"),
        note="Fundamental Duties came in by the 42nd Amendment, 1976.",
    ),
    SeededError(
        key="dpsp-part",
        category="factual",
        sentence=("The Directive Principles of State Policy are contained in "
                  "Part III of the Constitution and are justiciable in nature."),
        must_mention=("part iv", "directive"),
        note="DPSPs are Part IV, and are non-justiciable.",
    ),
    # ----------------------------------------------------------------- history
    SeededError(
        key="plassey-year",
        category="factual",
        sentence=("The Battle of Plassey was fought in 1764 and established "
                  "British control over Bengal."),
        must_mention=("plassey", "1757"),
        note="Plassey was 1757; 1764 was Buxar.",
    ),
    SeededError(
        key="dandi-year",
        category="factual",
        sentence=("Mahatma Gandhi began the Dandi March in March 1932 to "
                  "protest against the salt tax."),
        must_mention=("dandi", "1930"),
        note="The Dandi March began in March 1930.",
    ),
    SeededError(
        key="sushruta",
        category="factual",
        sentence=("Sushruta was a renowned mathematician of ancient India who "
                  "is remembered for his work on algebra."),
        must_mention=("sushruta", "surg"),
        note="Sushruta was a surgeon; the Sushruta Samhita is a medical text.",
    ),
    # --------------------------------------------------------------- geography
    SeededError(
        key="prambanan",
        category="factual",
        sentence=("The Prambanan temple complex, an example of Indian cultural "
                  "influence abroad, is located in Malaysia."),
        must_mention=("prambanan", "indonesia"),
        note="Prambanan is in Java, Indonesia.",
    ),
    SeededError(
        key="tropic-states",
        category="factual",
        sentence=("The Tropic of Cancer passes through ten Indian states, "
                  "entering the country in Gujarat."),
        must_mention=("tropic of cancer", "eight"),
        note="The Tropic of Cancer passes through 8 states.",
    ),
    # ------------------------------------------------------------- conceptual
    SeededError(
        key="contradiction-tribunals",
        category="conceptual",
        sentence=("Administrative tribunals were established by the 42nd "
                  "Amendment, and Article 323A empowers Parliament alone to "
                  "constitute them. Elsewhere these notes state that "
                  "administrative tribunals were created by the 44th Amendment."),
        must_mention=("contradict",),
        note="The note contradicts itself about which amendment created tribunals.",
    ),
    SeededError(
        key="definition-secularism",
        category="conceptual",
        sentence=("Secularism in the Indian Constitution means that the State "
                  "promotes one official religion while tolerating the others."),
        must_mention=("secular",),
        note="Indian secularism means no official religion and equal treatment.",
    ),
    # ---------------------------------------------------------------- language
    SeededError(
        key="spelling-healthy",
        category="language",
        sentence=("A helthy democracy depends on an informed citizenry and on "
                  "institutions that answer for their decisions."),
        must_mention=("helthy",),
        note="'helthy' is a misspelling of 'healthy'.",
    ),
    SeededError(
        key="proper-noun-ashoka",
        category="language",
        sentence=("Asoka embraced Buddhism after the Kalinga war, and the "
                  "edicts of Ashoka were inscribed on pillars across the "
                  "empire, so Asoka is remembered as a patron of the faith."),
        must_mention=("asoka",),
        note="The same name is spelled 'Asoka' and 'Ashoka' in one passage.",
    ),
)

#: Correct sentences, mixed in so the document is not all errors. A checker that
#: flags these is producing false positives, which is how precision is measured.
CLEAN_SENTENCES: tuple[str, ...] = (
    "The Right to Information Act gives every citizen a legal route to hold a "
    "public authority to account for what it decided and why.",
    "Probity in governance means adherence to moral principles by a public "
    "servant, and it is measured by outcomes rather than by stated intent.",
    "A citizen's charter states what service a department will provide and "
    "within what time, so that a delay becomes visible and answerable.",
    "The Second Administrative Reforms Commission recommended that every "
    "department publish measurable service standards against which it is judged.",
    "Social audit brings the beneficiaries of a scheme into the verification "
    "process, which makes a paper record harder to falsify than an audit alone.",
    "A public servant who declares a conflict of interest before a decision "
    "protects both the decision and their own standing in the service.",
    "Transparency reduces the space in which discretion can be exercised "
    "arbitrarily, and that reduction is what makes accountability enforceable.",
    "The Preamble declares India to be a sovereign, socialist, secular and "
    "democratic republic, and it was amended by the 42nd Amendment in 1976.",
    "Judicial review allows the courts to examine whether a law is consistent "
    "with the Constitution, and it is part of the basic structure.",
    "The Comptroller and Auditor General audits the accounts of the Union and "
    "the States and reports to the appropriate legislature.",
)


def by_category() -> dict[str, list[SeededError]]:
    out: dict[str, list[SeededError]] = {}
    for error in SEEDED:
        out.setdefault(error.category, []).append(error)
    return out
