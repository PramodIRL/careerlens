"""A developer-authored synthetic retrieval benchmark (Prompt 5.3).

WHAT THIS IS NOT. It is NOT real-world hiring validation, and no number
derived from it may be described that way. These are twelve invented
cases written by the same person building the feature, which is a known
and serious bias: the queries and the evidence were authored together,
so the model is being graded on text chosen by someone who already knew
what it should retrieve. Nothing here has been reviewed by a recruiter,
drawn from real postings, or labelled by an independent annotator.

WHAT IT IS FOR. Catching direction-of-travel problems that are otherwise
invisible: a flipped distance sign, a provider swap that degrades
ranking, a threshold that excludes the very cases the feature exists to
find. It answers "is this behaving the way we think" — never "is this
good enough for people".

ALL CONTENT IS FICTIONAL. No real person, company, product name used as
an employer, or real posting appears. tests/test_retrieval_evaluation.py
asserts that.

THE FOUR LABELS, and why each is here:

  relevant_paraphrase  Genuinely relevant, sharing NO significant word
                       with the query. This is the case semantic search
                       exists for and keyword search cannot serve.
  relevant_lexical     Genuinely relevant AND sharing query words. The
                       baseline — keyword matching would find these too,
                       so they measure nothing new on their own.
  lexical_distractor   Shares query words but is NOT relevant ("pet
                       python", "shipping containers"). The trap: a
                       system rewarding surface overlap ranks these
                       highly, and no similarity threshold separates
                       them from real paraphrases.
  unrelated            Neither. Establishes where the floor sits.
"""

from dataclasses import dataclass, field
from enum import StrEnum

# Bumped whenever a case is added, removed or reworded. Reported
# alongside every metric, because a score without the dataset that
# produced it cannot be compared against anything.
DATASET_VERSION = "retrieval-eval-v1"


class Label(StrEnum):
    RELEVANT_PARAPHRASE = "relevant_paraphrase"
    RELEVANT_LEXICAL = "relevant_lexical"
    LEXICAL_DISTRACTOR = "lexical_distractor"
    UNRELATED = "unrelated"


def is_relevant(label: Label) -> bool:
    """Ground truth. Only the two `relevant_*` labels count; a lexical
    distractor is explicitly NOT relevant however high it scores."""
    return label in (Label.RELEVANT_PARAPHRASE, Label.RELEVANT_LEXICAL)


@dataclass(frozen=True)
class EvidenceItem:
    """One piece of fictional candidate evidence, with its ground-truth
    label. `duplicate_group` marks near-duplicates — the same work
    described twice, as happens when a resume and a README both cover
    one project."""

    text: str
    label: Label
    duplicate_group: str | None = None


@dataclass(frozen=True)
class EvalCase:
    """One job-side query and the evidence pool it is scored against."""

    query: str
    items: tuple[EvidenceItem, ...] = field(default_factory=tuple)


def _p(text: str, group: str | None = None) -> EvidenceItem:
    return EvidenceItem(text, Label.RELEVANT_PARAPHRASE, group)


def _l(text: str, group: str | None = None) -> EvidenceItem:
    return EvidenceItem(text, Label.RELEVANT_LEXICAL, group)


def _d(text: str) -> EvidenceItem:
    return EvidenceItem(text, Label.LEXICAL_DISTRACTOR)


def _u(text: str) -> EvidenceItem:
    return EvidenceItem(text, Label.UNRELATED)


BENCHMARK: tuple[EvalCase, ...] = (
    EvalCase(
        "Experience with container orchestration",
        (
            _p("Built Kubernetes-based microservices"),
            _p("Managed pod autoscaling and Helm release rollouts"),
            _l("Ran container orchestration for a staging cluster"),
            _d("Organised shipping containers at a freight depot"),
            _u("Wrote marketing copy for a bakery newsletter"),
            _u("Taught piano lessons to beginners"),
        ),
    ),
    EvalCase(
        "Strong Python programming skills required",
        (
            _l("Wrote data pipelines and automation scripts in Python"),
            _p("Built a Django web service with pytest coverage"),
            _d("Cared for a pet python and other reptiles"),
            _u("Designed posters for a student theatre group"),
            _u("Managed a coffee shop inventory spreadsheet"),
        ),
    ),
    EvalCase(
        "Familiarity with relational databases and SQL",
        (
            _p("Designed normalised schemas and tuned slow queries"),
            _l("Wrote complex SQL joins against PostgreSQL"),
            _d("Built dashboards in Excel for the sales team"),
            _u("Led a weekly yoga class at the community centre"),
        ),
    ),
    EvalCase(
        "Experience building REST APIs",
        (
            _p("Implemented HTTP endpoints with FastAPI and OpenAPI docs"),
            _l("Built and documented REST APIs for a mobile client"),
            _d("Rested between marathon training sessions"),
            _u("Volunteered at an animal shelter on weekends"),
        ),
    ),
    EvalCase(
        "Frontend development with modern JavaScript frameworks",
        (
            _p("Built single-page interfaces in React with TypeScript"),
            _p("Developed a Next.js dashboard with server components"),
            _l("Used JavaScript frameworks to build the company site"),
            _u("Studied French literature and modern poetry"),
        ),
    ),
    # --- duplicate crowding: one project, described three ways --------
    EvalCase(
        "Continuous integration and automated testing",
        (
            _p("Set up GitHub Actions to run the suite on every push", "ci-pipeline"),
            _p("Configured a CI workflow that runs all tests on each commit", "ci-pipeline"),
            _l("Maintained CI pipelines and automated test runs", "ci-pipeline"),
            _p("Wrote unit and integration tests with pytest"),
            _d("Integrated into a new team after relocating"),
            _u("Collected vinyl records from the 1970s"),
        ),
    ),
    EvalCase(
        "Cloud infrastructure and deployment experience",
        (
            _p("Provisioned AWS resources with Terraform"),
            _l("Deployed services to production on a cloud provider"),
            _d("Photographed clouds for a landscape series"),
            _u("Ran a book club for twelve members"),
        ),
    ),
    EvalCase(
        "Machine learning model development",
        (
            _p("Trained and evaluated classifiers with scikit-learn"),
            _p("Built a recommendation model and measured its accuracy"),
            _l("Developed machine learning models for forecasting"),
            _d("Repaired sewing machines as a side business"),
            _u("Hiked the coastal trail every summer"),
        ),
    ),
    EvalCase(
        "Version control and code review practices",
        (
            _p("Reviewed pull requests and kept a clean commit history"),
            _l("Used Git for version control across a team of six"),
            _d("Reviewed restaurants for a local food blog"),
            _u("Restored a vintage bicycle over one winter"),
        ),
    ),
    # --- duplicate crowding: the same resume line and README line -----
    EvalCase(
        "Building data pipelines at scale",
        (
            _p("Built an ETL pipeline moving millions of rows nightly", "etl-job"),
            _p("Wrote a nightly batch job that transforms and loads records", "etl-job"),
            _l("Designed data pipelines processing large daily volumes"),
            _d("Fitted new plumbing pipelines in a residential block"),
            _u("Trained for a half marathon in the spring"),
        ),
    ),
    EvalCase(
        "Experience with agile development processes",
        (
            _p("Worked in two-week sprints with daily stand-ups"),
            _l("Followed agile development practices on a scrum team"),
            _d("Was praised for an agile and nimble climbing style"),
            _u("Painted watercolour landscapes as a hobby"),
        ),
    ),
    EvalCase(
        "Monitoring and incident response",
        (
            _p("Set up dashboards and alerting for production services"),
            _p("Carried the on-call pager and wrote post-mortems"),
            _l("Handled incident response and monitored system health"),
            _d("Monitored water quality at a fish hatchery"),
            _u("Learned conversational Italian over two years"),
        ),
    ),
)


def total_items() -> int:
    return sum(len(case.items) for case in BENCHMARK)
