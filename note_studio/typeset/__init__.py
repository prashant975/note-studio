"""Typeset notes: a lecture PDF becomes A4 notes in the B.Sc. Wallah layout
(the look of the sample notes the team typeset in LaTeX), made in this process.

    prompts.py     what PW AI is asked (the engine's own prompt; REVISION names it)
    document.py    the JSON it writes, tidied; inline markup
    ai.py          the PW AI calls (notes, review, figures, label check) and their cost
    formulas.py    LaTeX formulas in Computer Modern (matplotlib mathtext)
    diagrams.py    trees and flows drawn as vectors
    render.py      the PDF: page furniture, boxes, tables, figures, pagination
    quality.py     the checks behind the run's quality report

note_studio/engines/typeset.py is the adapter New notes calls.
"""
