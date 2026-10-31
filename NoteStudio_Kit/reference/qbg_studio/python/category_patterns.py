"""Which question patterns each QBG category carries, and what it has been given.

Two sources, one answer. The REGISTRY is what is known about a category
before anything is uploaded: which of QBG's question types its papers hold,
how options and answer keys are labelled, what on the page is not a
question. It is a dictionary keyed by the category's exact QBG name (the
names in exam_categories), each pointing at a FAMILY of exams that share a
shape; a name that is not listed falls back to a keyword match on the
family, and a test or dummy category gets nothing. A dictionary because the
lookup is by name and happens on every read; the families are shared so
that a correction reaches every category of that kind at once.

The second source is what has actually been uploaded: every question
created on QBG records its category and type here (the category_patterns
table, written by the creation job). The reader's briefing quotes that mix,
and the type check after reading accepts a type the category has been given
before, so a category's profile grows with use instead of needing a code
change for each new kind of paper (the operator's ask, 25 September 2026).
"""
import re
import time

from question_types import TYPE_LABELS

FAMILIES = {
    'neet-jee': dict(
        label='NEET / JEE',
        types=('SCQ', 'NUMERICAL', 'MCQ', 'INTEGER', 'AR', 'ML', 'COMP'),
        key='numeric',
        summary='single-correct, numerical, matching-list, multiple-correct and passage questions; '
                'answer keys give option numbers 1-4',
        # JEE batches, 25 September 2026: told "every question is SCQ or
        # numerical", the reader made a matching list an SCQ and a passage a
        # NUMERICAL, and it wrote 1 for an answer printed 1.2.
        hint=('This is a NEET/JEE-style paper. Most questions are a single-correct choice with four options '
              'numbered (1)-(4) (SCQ), or a numerical-answer question with no options (NUMERICAL). JEE papers '
              'also carry: MATCHING LISTS - "Match List-I with List-II" or Column-I/Column-II with four '
              'combination options such as (A)-(III), (B)-(I), (C)-(IV), (D)-(II): type ML, both lists kept as '
              'a table in the question, the four combinations as options A-D, never SCQ. MULTIPLE-CORRECT '
              'questions ("one or more options are correct"): MCQ. PASSAGES - a paragraph or comprehension '
              'followed by the two or three questions that depend on it, in any subject: ONE COMP record with '
              'those questions as its children, each child keeping its own type (SCQ or NUMERICAL), never a '
              'NUMERICAL or SCQ record holding the passage. A NUMERICAL answer is copied exactly as printed '
              'with its sign and every decimal digit (1.2 stays 1.2, never 1); use INTEGER only where the '
              'section says the answer is an integer. There are no subjective questions. Answer keys give '
              'option numbers 1-4; convert them to A-D. Section instructions and marking schemes are not '
              'questions.')),
    'clat': dict(
        label='CLAT / law entrance',
        types=('COMP', 'SCQ'),
        key='alpha',
        summary='passages with their own questions (COMP groups); options and keys lettered A-D',
        hint=('This is a CLAT-style paper: it is built from passages, each followed by its own questions. '
              'Return every passage as ONE COMP record whose children are the questions that depend on it, '
              'in printed order, with options lettered (A)-(D). A standalone question without a passage is '
              'an SCQ. Answer keys use letters.')),
    'defence': dict(
        label='Defence (NDA / CDS / AFCAT)',
        types=('SCQ',),
        key='alpha',
        summary='single-correct questions with options (a)-(d); negative-marking notes are not questions',
        hint=('This is a Defence (NDA/CDS/AFCAT-style) paper: every question is a single-correct choice '
              'with four options lettered (a)-(d). Negative-marking notes, directions and section headings '
              'are not questions. Answer keys use letters.')),
    'foundation': dict(
        label='Foundation / school',
        types=('SUB', 'SCQ', 'MCQ', 'TF', 'NUMERICAL', 'FIB', 'AR', 'ML', 'COMP'),
        key='alpha',
        summary='subjective questions with lettered parts kept together; marks are not question text',
        hint=('This is a school/foundation paper: expect subjective questions, some with lettered parts '
              '(a), (b), (c) or (i), (ii). Keep a question and all its parts together as ONE SUB record; '
              'never split the parts into separate questions. Marks in brackets are not part of the '
              'question text. Objective questions use lettered options; a case study or passage with its '
              'own questions is ONE COMP record with the questions as children; fill in the blanks (FIB), '
              'true/false (TF), assertion-reason (AR) and match the following (ML) keep their own types.')),
    'entrance': dict(
        label='Objective entrance / olympiad',
        types=('SCQ', 'MCQ', 'NUMERICAL', 'AR', 'ML', 'COMP', 'TF'),
        key='alpha',
        summary='single-correct questions with lettered options; some numerical, multiple-correct and passage sets',
        hint=('This is an objective entrance or olympiad-style paper (Olympiad, JNVST, AISSEE, SAT, CUET): '
              'single-correct questions with four lettered options, sometimes numerical-answer or '
              'multiple-correct items, and passage-based sets (ONE COMP record with its questions as '
              'children). Answer keys use letters or option numbers; convert numbers to A-D. Instructions '
              'and marking notes are not questions.')),
    'pg': dict(
        label='PG entrance (GATE / ESE / NET / JAM / CUET PG / NEET PG / MBA)',
        types=('SCQ', 'MCQ', 'NUMERICAL', 'INTEGER', 'ML', 'AR', 'COMP'),
        key='alpha',
        summary='single-correct, multiple-select, numerical (decimals), matching and data-set questions',
        hint=('This is a PG entrance / national-level test paper (GATE, ESE, NET, JAM, CUET PG, NEET PG, '
              'MBA): single-correct (SCQ), multiple-select MSQ (type MCQ), numerical-answer questions '
              '(NUMERICAL; decimals allowed, copy every digit as printed), matching (ML with four '
              'combination options), assertion-reason (AR) and passage or data sets (ONE COMP record with '
              'the questions as children). Options are lettered (A)-(D); answer keys use letters or '
              'numbers. Section instructions and marks are not questions.')),
    'government': dict(
        label='Government / competitive (SSC, Banking, Railway, PSC, UPSC, TET)',
        types=('SCQ', 'MCQ', 'AR', 'ML', 'COMP', 'FIB', 'TF', 'SUB'),
        key='alpha',
        summary='single-correct questions with lettered options; statement, assertion-reason, matching and passage sets',
        hint=('This is a government / competitive exam paper (SSC, Banking, Railway, State PSC, UPSC, TET, '
              'AE/JE, Judiciary): single-correct questions with four lettered options, plus statement-based '
              'items, assertion-reason (AR), match the following (ML) and passage sets - reading '
              'comprehension, data interpretation, cloze tests - each ONE COMP record with the questions as '
              'children. A bilingual paper prints each question in Hindi as well: keep the language as '
              'read, do not translate. Answer keys use letters or numbers 1-4. Directions and marking '
              'notes are not questions.')),
    'commerce': dict(
        label='Commerce / finance (CA, CFA, ACCA, banking certification, B.Com, MBA)',
        types=('SCQ', 'MCQ', 'TF', 'SUB', 'NUMERICAL', 'COMP', 'FIB'),
        key='alpha',
        summary='single-correct questions, true/false, numerical computations, case studies and descriptive questions',
        hint=('This is a commerce / finance paper (CA, CFA, ACCA, banking certification, B.Com, MBA): '
              'single-correct questions with four lettered options, true/false (TF), numerical computations '
              '(NUMERICAL, with decimals and units as printed), case-study passages with their questions '
              '(ONE COMP record with the questions as children) and descriptive questions (SUB). Answer '
              'keys use letters.')),
    'medical-allied': dict(
        label='Nursing / paramedical / pharmacy / agriculture',
        types=('SCQ', 'MCQ', 'AR', 'ML', 'TF', 'FIB', 'SUB'),
        key='alpha',
        summary='single-correct questions with lettered options; some assertion-reason, matching and true/false items',
        hint=('This is a nursing / paramedical / pharmacy / agriculture-style paper: single-correct '
              'questions with four lettered options, sometimes assertion-reason (AR), match the following '
              '(ML), true/false (TF) and fill-in-the-blank (FIB) items. Answer keys use letters or numbers '
              '1-4. Instructions are not questions.')),
    'skills': dict(
        label='Skills / aptitude / design entrance',
        types=('SCQ', 'MCQ', 'TF', 'FIB', 'SUB', 'COMP', 'NUMERICAL'),
        key='alpha',
        summary='single- or multiple-correct questions, short numerical answers, blanks, descriptive tasks',
        hint=('This is a skills / aptitude / design-entrance paper: single-correct or multiple-correct '
              'questions with four lettered options, short numerical answers, fill in the blanks (FIB), '
              'descriptive tasks (SUB) and passage-based sets (ONE COMP record with the questions as '
              'children). Answer keys use letters.')),
}

# Every QBG category by its exact name -> the family it belongs to. '' means
# the category is a test/dummy one or its papers are not known yet; it still
# learns from what is uploaded to it.
REGISTRY = {
    'NEET-JEE': 'neet-jee', 'NEET': 'neet-jee', 'JEE': 'neet-jee', 'IISER': 'neet-jee',
    'CLAT': 'clat', 'CLAT PG': 'clat',
    'Defence': 'defence',
    'Foundation': 'foundation', 'Boards': 'foundation', 'State Board': 'foundation',
    'School Curriculum': 'foundation', 'K-8': 'foundation', 'K5 Elementary': 'foundation',
    'Fundo': 'foundation', 'AISSE': 'foundation', 'Arts': 'foundation',
    'Olympiad': 'entrance', 'JNVST': 'entrance', 'AISSEE': 'entrance', 'SAT': 'entrance', 'CUET': 'entrance',
    'GATE': 'pg', 'ESE': 'pg', 'CSIR NET/IIT JAM': 'pg', 'IIT JAM': 'pg', 'CSIR NET/JRF': 'pg',
    'UGC NET': 'pg', 'CUET PG': 'pg', 'NEET PG': 'pg', 'NEET PG 2': 'pg', 'MBA': 'pg',
    'SSC': 'government', 'SSC-5 Options': 'government', 'Banking': 'government', 'Railway': 'government',
    'State PSC': 'government', 'UPSC': 'government', 'Sarrthi IAS - UPSC': 'government', 'TET': 'government',
    'BPSC-TET': 'government', 'AE/JE': 'government', 'Judiciary': 'government',
    'CA': 'commerce', 'CFA': 'commerce', 'ACCA': 'commerce', 'JAIIB/CAIIB': 'commerce', 'BFSI': 'commerce',
    'Commerce': 'commerce', 'B.Com': 'commerce', 'CS': 'commerce',
    'Nursing': 'medical-allied', 'Paramedical': 'medical-allied', 'Paramedical Category': 'medical-allied',
    'Pharmacy': 'medical-allied', 'B.Sc': 'medical-allied', 'Agriculture': 'medical-allied',
    'PW Skills': 'skills', 'Pi Skills': 'skills', 'Earners (Upskilling)': 'skills',
    'Design and Architecture': 'skills',
    # Not known yet, or not real exams: no briefing, only what is uploaded.
    'Acadfly': '', 'Xylem': '', 'Utkarsh': '', 'Vernacular': '', 'REAL Test': '', 'RankUp Test Series': '',
    'Dummy Catgeory(Sanity)': '', 'Tag Cleanup [Dummy]': '', 'Dummy category(Sanity2)': '',
    'testing cat': '', 'sanity test': '', 'testing ec': '', 'test stream': '', 'TEST': '',
}
_BY_NAME = {name.casefold(): family for name, family in REGISTRY.items()}

# A name that is not in the registry (a renamed or a new category): the
# first family whose words appear in it wins, so narrower exams come first.
KEYWORDS = [
    ('clat', re.compile(r'\b(clat|ailet|law|llb|legal)\b', re.I)),
    ('defence', re.compile(r'\b(nda|cds|afcat|defence|defense|ssb|capf|inet)\b', re.I)),
    ('foundation', re.compile(r'\b(foundation|icse|cbse|school|class\s*\d+|board|boards)\b', re.I)),
    ('entrance', re.compile(r'\b(olympiad|ntse|sat|cuet|jnvst|aissee|sainik|navodaya)\b', re.I)),
    ('neet-jee', re.compile(r'\b(neet|jee|iit|aiims|medical|engineering|iiser)\b', re.I)),
    ('pg', re.compile(r'\b(gate|ese|net|jrf|jam|cat|mba|pg)\b', re.I)),
    ('government', re.compile(r'\b(ssc|bank|banking|railway|rrb|psc|upsc|ias|tet|ctet|je|ae|judiciary|police)\b', re.I)),
    ('commerce', re.compile(r'\b(ca|cfa|acca|cma|commerce|b\.?com|jaiib|caiib|bfsi|finance)\b', re.I)),
    ('medical-allied', re.compile(r'\b(nursing|paramedical|pharmacy|pharma|b\.?sc|agriculture|icar)\b', re.I)),
    ('skills', re.compile(r'\b(skills?|upskilling|design|architecture|nata|coding)\b', re.I)),
]


def _name(category):
    return str(category or '').strip()


def family_for(category):
    """The family key for a category, or '' when none applies."""
    name = _name(category)
    if not name:
        return ''
    if name.casefold() in _BY_NAME:
        return _BY_NAME[name.casefold()]
    for key, pattern in KEYWORDS:
        if pattern.search(name):
            return key
    return ''


def family(category):
    """The family's profile dict for a category, or None."""
    return FAMILIES.get(family_for(category))


# ------------------------------------------------------- what was uploaded --
def initialize():
    import db
    con = db.connect()
    try:
        con.execute('CREATE TABLE IF NOT EXISTS category_patterns (category TEXT NOT NULL, qtype TEXT NOT NULL, '
                    'count INTEGER NOT NULL DEFAULT 0, last_seen REAL, PRIMARY KEY(category, qtype))')
    finally:
        con.close()


def record(category, qtype, count=1):
    """One more question of this type created on QBG in this category."""
    import db
    name, kind = _name(category), str(qtype or '').strip().upper()
    if not name or kind not in TYPE_LABELS or int(count) <= 0:
        return
    initialize()
    con = db.connect()
    try:
        con.execute('INSERT INTO category_patterns(category, qtype, count, last_seen) VALUES (?, ?, ?, ?) '
                    'ON CONFLICT(category, qtype) DO UPDATE SET count = count + excluded.count, '
                    'last_seen = excluded.last_seen', (name, kind, int(count), time.time()))
    finally:
        con.close()


def observed(category):
    """{qtype: count} of what this category has been given, most common first."""
    name = _name(category)
    if not name:
        return {}
    try:
        import db
        initialize()
        con = db.connect()
        try:
            rows = con.execute('SELECT qtype, count FROM category_patterns WHERE category = ? ORDER BY count DESC, qtype',
                               (name,)).fetchall()
        finally:
            con.close()
    except Exception:
        return {}      # no database here (a test, a build check): nothing observed
    return {str(r[0]): int(r[1]) for r in rows if int(r[1]) > 0}


def observed_line(category):
    """One sentence for the reader about the category's uploads so far, or ''."""
    mix = observed(category)
    if not mix:
        return ''
    total = sum(mix.values())
    parts = ', '.join('%s %d%%' % (kind, max(1, round(100 * count / total))) for kind, count in mix.items())
    return ('Questions already uploaded to QBG in the "%s" category (%d so far): %s. Expect the same shapes '
            'in this paper unless it clearly says otherwise.' % (_name(category), total, parts))


def expected_types(category):
    """The types this category is known to carry: its family's, plus any it has been given."""
    known = list((family(category) or {}).get('types') or ())
    for kind in observed(category):
        if kind not in known:
            known.append(kind)
    return tuple(known)
