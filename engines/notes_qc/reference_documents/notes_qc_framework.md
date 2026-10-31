# PW OnlyIAS — Notes QC Framework

> **This is the authoritative policy document for this app.** It is the
> human-readable source that `config/upsc_qc_rules.yaml` and
> `config/upsc_qc_sources.yaml` encode, and that
> `src/qc_prompts.py` renders into every AI checking prompt.
>
> Source: *DCN AI REPORT* (PW OnlyIAS, Daily Class Notes).
> Saved here on 2026-08-21. **Do not paraphrase when editing** — if the policy
> changes, change this file and the two YAMLs together, then run
> `pytest tests/test_framework_alignment.py`.

Errors in the notes can be classified into the following three categories:

1. **Factual Errors** — Incorrect facts, data, dates, names, figures, or
   information that are not consistent with authentic sources.
2. **Conceptual and Flow Errors** — Incorrect explanation, interpretation,
   logic, or understanding of a concept, process, or principle.
3. **English and Grammatical Errors** — Errors related to grammar, spelling,
   punctuation, sentence structure, word usage, or clarity of language.

---

## Factual Error

1. **Article/Schedule/Amendment/judgment verification:** Every Article,
   Schedule, Amendment, or Supreme Court judgment checked against standard
   texts (Laxmikanth, D.D. Basu, Bare Acts); any misquote or imprecise legal
   phrasing flagged.
   a. 100% of legal/constitutional references match the cited standard source
      verbatim; zero unverified citations remain in the final note.

2. **Authentic-source verification:** Only authentic sites referenced;
   Wikipedia or coaching-institute material/websites never treated as standard
   sources.
   a. No claim is sourced solely from Wikipedia, blogs, or coaching-institute
      material; only official (.gov), standard textbook, or institutional
      sources are used. Any other source needs to get flagged.

3. **Statistical/data cross-referencing:** Economic statistics, demographic
   data, and global rankings checked against institutional sources (Economic
   Survey, Union Budget, NITI Aayog, IMF, World Bank, Census, etc.); outdated
   figures flagged.
   a. All figures match the latest published edition of the relevant
      institutional source; no outdated data point is finalized without a flag.

4. **Unsourced-data flagging:** Any data for which no authentic source can be
   found is flagged rather than left unverified.

5. **Subject-specific standard-source matching:** Facts checked against the
   exact standard book prescribed for that subject (e.g., R.S. Sharma for
   Ancient History, Satish Chandra for Medieval History, Bipan
   Chandra/Spectrum for Modern History, G.C. Leong for Geography, Ramesh
   Singh/Vivek Singh for Economy, Nitin Singhania for Art & Culture, etc.)
   rather than a generic or unrelated source.

6. **Consistency of proper nouns / names:** Same person, place, dynasty, or
   text spelled identically throughout the note.

---

## Concept/Flow Error

1. **Heading–content mismatch:** Verify that all content under a heading falls
   strictly within its stated scope — neither broader nor narrower. Flag any
   deviation.

2. **Internal contradiction between sections:** Verify that every core fact or
   definition is stated consistently wherever it recurs in the document —
   including within tables. Flag any restatement that contradicts or diverges
   from its original form.

3. **Incomplete explanation:** Every definition or equivalence is accompanied
   by a plain-language explanation a student could restate independently. If
   otherwise, flag immediately.

4. **Formatting check:** Every table, list, and formatting element displays
   exactly as intended in the final student-facing output, with no raw syntax
   or broken formatting visible anywhere in the note.

---

## English/Grammar Error

1. **Spelling error:** Checking the note's title and running headers for typos,
   since they are the most visible part of the page. Same name spelled
   differently across the document to be highlighted.

2. **Grammatical accuracy:** Zero grammatical errors that affect sentence
   meaning or clarity remain in the final note.

3. **Meaningful sentences:** Every sentence in the notes should be
   independently understandable without relying on the previous or next
   sentence.
   a. Use simple, clear, and easy-to-understand language throughout the notes.
   b. Avoid complex vocabulary, lengthy sentence structures, or high-level
      English. Keep sentence formation straightforward and reader-friendly.

---

## Sources to be used

| विषय / Subject | Standard Book |
| --- | --- |
| भारतीय राजव्यवस्था (Polity) | M. Laxmikanth – Indian Polity · NCERT · Bare Act PM Bakshi · https://legislative.gov.in · https://lawcommissionofindia.nic.in · https://www.pib.gov.in/Allrel.aspx?reg=48&lang=1 |
| आधुनिक इतिहास (Modern History) | NCERT · Spectrum – A Brief History of Modern India · India's Struggle for Independence by Bipan Chandra |
| प्राचीन इतिहास (Ancient History) | R.S. Sharma – India's Ancient Past · NCERT · Ancient and Medieval India – Poonam Dalal Dahiya |
| मध्यकालीन इतिहास (Medieval History) | Satish Chandra – Medieval India · NCERT · Ancient and Medieval India – Poonam Dalal Dahiya |
| विश्व इतिहास (World History) | NCERT |
| Post Independence | NCERT Class 12th · India after Gandhi by Ramachandra Guha |
| भारतीय भूगोल (Geography) | G.C. Leong – Certificate Physical & Human Geography · NCERT |
| विश्व भूगोल (World Geography) | G.C. Leong · ATLAS MAP (Oxford) · NCERT |
| अर्थव्यवस्था (Economy) | NCERT · Ramesh Singh or Vivek Singh · Economic Survey · Union Budget · https://rbi.org.in · https://mospi.gov.in · https://dea.gov.in · https://finmin.gov.in |
| पर्यावरण (Environment) | Shankar IAS Environment · NCERT · https://moef.gov.in · https://cpcb.nic.in · https://ntca.gov.in · https://forestsurveyofindia.gov.in |
| विज्ञान एवं प्रौद्योगिकी (Science & Tech) | NCERT + Current Affairs · Ravi P. Agrahari · https://isro.gov.in · https://dst.gov.in · https://dbtindia.gov.in · https://drdo.gov.in |
| कला एवं संस्कृति (Art & Culture) | Nitin Singhania + NCERT |
| आंतरिक सुरक्षा (Internal Security) | द्वितीय ARC रिपोर्ट · Challenges to Internal Security of India by Ashok Kumar |
| आपदा प्रबंधन (Disaster Management) | NDMA Guidelines · https://ndma.gov.in · https://imd.gov.in |
| Governance | 2nd ARC Report · RTI.gov.in |
| International Relations | http://mea.gov.in/ · http://www.idsa.in/ · http://www.mea.gov.in/overseas-indian-affairs.htm · Challenges to Indian foreign policy by Rajiv Sikri and McGraw hill book on it |
| Society and Social Justice | Indian society class 12th · Social Problems in India by Ram Ahuja · https://mohfw.gov.in · https://nhm.gov.in · https://nmc.org.in · https://socialjustice.gov.in · https://tribal.nic.in · https://wcd.gov.in |
| Agriculture | Niti Aayog reports on Doubling Farmers Income and issues @75 · Yojana Kurukshetra magazine · https://agricoop.nic.in · https://icar.org.in |
| नैतिकता (Ethics) | Lexicon for Ethics |
| Government schemes | India Year Book |
| निबंध (Essay) | Previous Year Essays + real example and philosophers |
