"""Fictional sample resumes, and the document builders that turn them
into real PDF/DOCX bytes (Prompt 2.5).

Run it with:
    make sample-resumes
(or: cd apps/api && uv run python -m scripts.sample_resumes)

WHY THIS EXISTS. Demonstrating the resume-to-skills flow through the real
UI needs an actual file to put in the upload input. Without a safe one to
hand, the obvious thing a developer reaches for is their own resume — a
real name, a real address, a real phone number — which then sits in a dev
database and on dev disk indefinitely. This module removes that
temptation: `make sample-resumes` writes fictional documents to
apps/api/var/samples/ (gitignored, so a generated document cannot be
committed), and the demo uses those.

WHAT THIS IS NOT. It writes FILES ONLY. It creates no users, no resumes,
no candidate skills and no evidence, and it never touches the database.
There is deliberately no "seed the demo state" path: inserting a
pre-made user with pre-made skills would bypass upload -> extraction ->
review, which is the exact flow a demo is supposed to show, and would
need a dev-only guard to avoid shipping a backdoor. See docs/decisions.md.

EVERY PERSON, EMPLOYER, ADDRESS AND CONTACT DETAIL BELOW IS INVENTED.
Emails use the `example.com` domain reserved by RFC 2606; phone numbers
use the 555-01xx range reserved for fiction; every employer and school is
made up. `tests/test_demo_end_to_end.py` asserts those two rules
mechanically, so a real resume pasted in here fails the test suite rather
than reaching a demo. Each document also states in its first line that it
is not a real person, so a file that escapes this repo still says so.

The same constants back both the generated demo files and the end-to-end
test, so what a demo shows and what CI proves cannot drift apart.
"""

import io
from dataclasses import dataclass
from pathlib import Path

from docx import Document

PDF_CONTENT_TYPE = "application/pdf"
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Stated in the document itself, not just in this file's comments: a
# generated file can be opened, mailed or attached far away from here,
# and it should say what it is wherever it lands.
FICTION_NOTICE = "Fictional sample resume - not a real person."

# Where `make sample-resumes` writes. Resolved from this file rather than
# from RESUME_STORAGE_DIR or the process's working directory: apps/api/var/
# is the directory .gitignore already covers, and pinning to it is what
# guarantees a generated document can never be committed by accident,
# whatever a developer has configured or wherever they run this from.
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "var" / "samples"


@dataclass(frozen=True)
class SampleResume:
    """One fictional resume: its text, and the format it is written in.

    `text` is the single source of truth — the PDF and DOCX builders both
    render it, so the file a demo uploads contains exactly the text the
    end-to-end test asserts on.
    """

    slug: str
    filename: str
    content_type: str
    text: str

    def build(self) -> bytes:
        """The document bytes, in this sample's declared format."""
        if self.content_type == PDF_CONTENT_TYPE:
            return build_pdf_bytes(self.text)
        return build_docx_bytes(self.text)


# A dense, unambiguous resume: the main demo document. Every skill here
# is a canonical taxonomy name (app/seeds/skill_taxonomy.py), so what the
# extractor finds is easy to check by eye against the page.
_BACKEND_ENGINEER = f"""{FICTION_NOTICE}

Ada Sample
Backend Engineer - Springfield, Fictionia
ada.sample@example.com | 555-0142 | example.com/ada-sample

SUMMARY
Early-career backend engineer with two years of experience building and
testing internal web services.

SKILLS
Languages: Python, SQL
Frameworks: FastAPI, Django
Databases: PostgreSQL, Redis
Tools: Docker, Git, pytest

EXPERIENCE
Junior Backend Engineer, Fictional Widgets Ltd (2024 - 2026)
- Built REST APIs with FastAPI and PostgreSQL for an internal reporting tool.
- Containerised the service with Docker and wrote unit tests in pytest.
- Reviewed teammates' changes and maintained the CI/CD pipeline.

EDUCATION
BSc Computer Science, University of Nowhere (2024)
"""

# The second format (DOCX), and the two matching behaviours worth seeing
# in a demo: "k8s" and "unit tests" are aliases, so their evidence
# records the lower alias confidence (0.75) next to canonical matches at
# 0.90; "nextjs" is neither a canonical name nor an alias, and resolves
# to Next.js purely through app/skill_matching.py's separator tolerance.
_FRONTEND_ENGINEER = f"""{FICTION_NOTICE}

Rio Placeholder
Frontend Engineer - Example City, Fictionia
rio.placeholder@example.com | 555-0177

SKILLS
Languages: TypeScript, JavaScript, HTML, CSS
Frameworks: React, nextjs
Testing: Jest, unit tests
Infrastructure: k8s, AWS

EXPERIENCE
Frontend Developer, Imaginary Interfaces Inc (2023 - 2026)
- Shipped a React and nextjs dashboard used by an internal support team.
- Wrote component tests with Jest and kept the suite green in CI/CD.
- Deployed the app to k8s on AWS.

EDUCATION
BA Design, Invented Polytechnic (2023)
"""

# A sparse resume whose prose contains two deliberate traps: "I go to a
# local coding meetup" and "express an interest" must NOT become the
# skills Go and Express. app/skill_matching.py only matches those two
# terms in list context, and this document is what makes that guarantee
# demonstrable in a live demo rather than only in a unit test.
_CAREER_CHANGER = f"""{FICTION_NOTICE}

Sam Invented
Career Changer - Example Town, Fictionia
sam.invented@example.com | 555-0163

SUMMARY
Former secondary school teacher moving into software. I go to a local coding
meetup most weeks, and express an interest in backend work in particular.

SKILLS
Languages: Python
Tools: Git

PROJECTS
Built a small Flask app to keep track of lesson plans and share them with
colleagues.

EDUCATION
PGCE, College of Make Believe (2019)
"""


SAMPLE_RESUMES: tuple[SampleResume, ...] = (
    SampleResume(
        slug="backend-engineer",
        filename="backend-engineer.pdf",
        content_type=PDF_CONTENT_TYPE,
        text=_BACKEND_ENGINEER,
    ),
    SampleResume(
        slug="frontend-engineer",
        filename="frontend-engineer.docx",
        content_type=DOCX_CONTENT_TYPE,
        text=_FRONTEND_ENGINEER,
    ),
    SampleResume(
        slug="career-changer",
        filename="career-changer.pdf",
        content_type=PDF_CONTENT_TYPE,
        text=_CAREER_CHANGER,
    ),
)


def _escape_pdf_text(line: str) -> bytes:
    r"""Escape one line for a PDF literal string.

    `\`, `(` and `)` are the three characters that would otherwise end or
    corrupt the string object. ASCII-only is enforced by the caller.
    """
    escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("ascii")


def build_pdf_bytes(body_text: str) -> bytes:
    """A minimal but *structurally valid* single-page PDF containing
    `body_text`, with an accurate xref table pypdf can actually parse —
    a real document, not just a "%PDF-" magic-byte stub (which is all
    tests/test_resume.py's upload-validation tests need).

    Each line of `body_text` is emitted as its own text-showing operator
    separated by `T*` (next line), so pypdf's extracted text keeps the
    original line structure. That matters beyond looking tidy:
    app/skill_matching.py builds each evidence excerpt from the line
    containing the match, so a document flattened to one long line would
    produce excerpts that are technically correct but unreadable in the
    UI — the opposite of what "show the evidence" is for.

    Hand-built rather than generated by a library: writing PDFs is not
    something this project needs a dependency for, and pypdf (already a
    dependency) only reads them.

    Raises ValueError on non-ASCII input. The base-14 Helvetica font used
    here is WinAnsi-encoded, so a UTF-8 byte sequence would silently come
    back out as mojibake — failing loudly beats generating a demo
    document with corrupted text in it.
    """
    if not body_text.isascii():
        raise ValueError("build_pdf_bytes supports ASCII text only")

    lines = body_text.split("\n")
    # 12pt type on 14pt leading, starting near the top of a US Letter page.
    stream_parts = [b"BT /F1 12 Tf 14 TL 72 720 Td"]
    for line in lines:
        stream_parts.append(b"(" + _escape_pdf_text(line) + b") Tj T*")
    stream_parts.append(b"ET")
    stream_body = b"\n".join(stream_parts)

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream_body) + stream_body + b"\nendstream",
        # /WinAnsiEncoding, not the font's built-in default: Helvetica's
        # built-in StandardEncoding maps 0x27 to "quoteright", so a plain
        # ASCII apostrophe would come back out of extraction as a curly
        # one and the text would no longer round-trip faithfully.
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]

    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(buf)
    buf += f"xref\n0 {len(objects) + 1}\n".encode()
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += (
        f"trailer\n<< /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref_offset}\n%%EOF"
    ).encode()
    return bytes(buf)


def build_docx_bytes(body_text: str) -> bytes:
    """A real DOCX containing `body_text`, one paragraph per line — which
    is how app/extraction.py reads a DOCX back (paragraph by paragraph),
    so the text round-trips with its line structure intact.
    """
    document = Document()
    for line in body_text.split("\n"):
        document.add_paragraph(line)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def write_samples(destination: Path = SAMPLES_DIR) -> list[Path]:
    """Write every sample resume to `destination`, returning the paths.

    Overwrites, so it is safe and deterministic to re-run: the documents
    are rendered from the constants above every time, never appended to
    or patched in place.
    """
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for sample in SAMPLE_RESUMES:
        path = destination / sample.filename
        path.write_bytes(sample.build())
        written.append(path)
    return written


def main() -> None:
    written = write_samples()
    for path in written:
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    print()
    print("These documents are fictional and contain no real personal data.")
    print("They are gitignored (apps/api/var/) — do not commit them.")
    print("Nothing was written to the database. See docs/demo.md.")


if __name__ == "__main__":
    main()
